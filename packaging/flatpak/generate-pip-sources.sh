#!/usr/bin/env bash
# Derive both architecture inputs from the checked-in universal resolution. No new resolution.
set -euo pipefail
cd "$(dirname "$0")/../.."
uv run --frozen --group build python packaging/generate_deps.py "$@"
