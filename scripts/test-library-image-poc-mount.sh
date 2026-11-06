#!/usr/bin/env bash
# One-time host mount, run by the user. The agent must not execute this script.
set -euo pipefail

if [[ $EUID != 0 || ${SUDO_UID:-0} == 0 || -z ${SUDO_GID:-} ]]; then
    echo 'Run this script through sudo from your normal user account.' >&2
    exit 1
fi

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
image="$repository/build/library-image-poc/storage.img"
volume="$repository/build/library-image-poc/volume"

[[ -f "$image" && ! -L "$image" && -d "$volume" && ! -L "$volume" ]]
[[ $(stat -c '%u' -- "$image") == "$SUDO_UID" ]]
[[ $(blkid -p -s TYPE -o value -- "$image") == btrfs ]]
if mountpoint -q -- "$volume"; then
    echo "Already mounted: $volume" >&2
    exit 1
fi
if [[ -n $(find "$volume" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
    echo "Mount directory must be empty: $volume" >&2
    exit 1
fi

mount -t btrfs -o loop,noatime,user_subvol_rm_allowed,nosuid,nodev -- "$image" "$volume"
chown -- "$SUDO_UID:$SUDO_GID" "$volume"
printf 'Mounted test image at %s for uid %s.\n' "$volume" "$SUDO_UID"
