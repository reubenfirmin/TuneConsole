#!/usr/bin/env bash
# Retry only hdiutil's transient busy failure; keep the signed app unchanged.
set -euo pipefail
SOURCE=$1
DESTINATION=$2

for attempt in 1 2 3; do
  if output=$(hdiutil create -volname "TuneConsole" -srcfolder "$SOURCE" -ov -format UDZO "$DESTINATION" 2>&1); then
    printf '%s\n' "$output"
    exit 0
  else
    status=$?
  fi
  printf '%s\n' "$output" >&2
  if [[ "$output" != *"hdiutil: create failed - Resource busy"* || "$attempt" == 3 ]]; then
    exit "$status"
  fi
  printf 'Disk image creation is busy; retrying (%s/3).\n' "$attempt" >&2
  sleep "$attempt"
done
