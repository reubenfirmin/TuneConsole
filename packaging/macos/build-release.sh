#!/usr/bin/env bash
# Run on an ephemeral macOS release runner after build.sh --prepare-only.
set -euo pipefail
cd "$(dirname "$0")/../.."

: "${RELEASE_TAG:?A release tag is required}"
: "${MACOS_SIGNING_IDENTITY:?Set MACOS_SIGNING_IDENTITY}"
: "${MACOS_TEAM_ID:?Set MACOS_TEAM_ID}"
: "${MACOS_CERTIFICATE_P12:?Set MACOS_CERTIFICATE_P12}"
: "${MACOS_CERTIFICATE_PASSWORD:?Set MACOS_CERTIFICATE_PASSWORD}"
: "${MACOS_NOTARY_APPLE_ID:?Set MACOS_NOTARY_APPLE_ID}"
: "${MACOS_NOTARY_APP_PASSWORD:?Set MACOS_NOTARY_APP_PASSWORD}"
if [[ ! "$MACOS_TEAM_ID" =~ ^[A-Z0-9]{10}$ ||
      "$MACOS_SIGNING_IDENTITY" != "Developer ID Application: "*" ($MACOS_TEAM_ID)" ]]; then
  echo "MACOS_SIGNING_IDENTITY must name a Developer ID Application certificate for MACOS_TEAM_ID" >&2
  exit 1
fi

signing_dir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/tuneconsole-signing.XXXXXX")
keychain="$signing_dir/signing.keychain-db"
previous_keychains=()
while IFS= read -r path; do
  previous_keychains+=("$path")
done < <(security list-keychains -d user | sed -E 's/^ *"(.+)"$/\1/')
cleanup() {
  security list-keychains -d user -s "${previous_keychains[@]}" || true
  security delete-keychain "$keychain" || true
  rm -rf "$signing_dir"
}
trap cleanup EXIT

keychain_password=$(openssl rand -hex 32)
security create-keychain -p "$keychain_password" "$keychain"
security set-keychain-settings -lut 1800 "$keychain"
security unlock-keychain -p "$keychain_password" "$keychain"
(umask 077; printf '%s' "$MACOS_CERTIFICATE_P12" | base64 -D -o "$signing_dir/certificate.p12")
security import "$signing_dir/certificate.p12" -k "$keychain" -P "$MACOS_CERTIFICATE_PASSWORD" \
  -T /usr/bin/codesign -T /usr/bin/security
rm "$signing_dir/certificate.p12"
security set-key-partition-list -S apple-tool:,apple: -s -k "$keychain_password" "$keychain"
security find-identity -v -p codesigning "$keychain" | grep -F "\"$MACOS_SIGNING_IDENTITY\""
security list-keychains -d user -s "$keychain" "${previous_keychains[@]}"
xcrun notarytool store-credentials TuneConsole --keychain "$keychain" \
  --apple-id "$MACOS_NOTARY_APPLE_ID" --team-id "$MACOS_TEAM_ID" --password "$MACOS_NOTARY_APP_PASSWORD"
unset MACOS_CERTIFICATE_P12 MACOS_CERTIFICATE_PASSWORD MACOS_NOTARY_APPLE_ID MACOS_NOTARY_APP_PASSWORD

arch=$(uname -m)
packaging/macos/build.sh --build-only
dmg="TuneConsole-macos-$arch.dmg"
cp packaging/macos/dist/TuneConsole-*.dmg "$dmg"
python3 packaging/macos/notarize.py "$dmg" "$arch" "$MACOS_TEAM_ID" "$keychain"
