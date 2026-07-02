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
  # The tool now ships as a PEP 723 script named flatpak-pip-generator.py (the old bare name is a
  # stub). uv run resolves its inline deps (requirements-parser) and executes it.
  curl -fsSL -o /tmp/flatpak-pip-generator.py \
    https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator.py
  GEN="uv run /tmp/flatpak-pip-generator.py"
fi

# Keep this list in sync with [project.dependencies] in ../../pyproject.toml (minus tomli, which is
# only needed on Python < 3.11 — the runtime ships a newer Python). uvicorn needs the [standard]
# extra so websockets is vendored (the /bridge/ws extension endpoint 404s without it), and numpy is
# a hard runtime dep of the recommender — both must be present or the offline build ships broken.
# These deps ship compiled extensions (C/Rust). Use prebuilt manylinux wheels instead of building
# them from sdists in the sandbox — the SDK has no Rust/maturin (pydantic-core, watchfiles) or the
# heavy build stacks (numpy). --wheel-arches defaults to x86_64,aarch64, matching Flathub targets.
$GEN --runtime="org.freedesktop.Sdk//$RUNTIME_VERSION" \
  --output python3-requirements \
  --prefer-wheels=numpy,pydantic-core,rapidfuzz,watchfiles,uvloop,httptools,markupsafe,pyyaml \
  "ytmusicapi>=1.12,<2" "fastapi>=0.110" "uvicorn[standard]>=0.29" "jinja2>=3.1" \
  "python-multipart>=0.0.9" "rapidfuzz>=3.6" "numpy>=1.26"

echo "Wrote $(pwd)/python3-requirements.json"
