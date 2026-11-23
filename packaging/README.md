# Packaging

TuneConsole ships as Linux Flatpak bundles, macOS apps/DMGs and Linux container
images, each for x86-64 and ARM64. Desktop launchers start the local server and
open the browser. All formats need the [browser extension](../extension/README.md).

| Format | Data location |
| --- | --- |
| Flatpak | `~/.var/app/com.tuneconsole.TuneConsole/{config,data}` |
| macOS | `~/Library/Application Support/TuneConsole`; logs in `~/Library/Logs/TuneConsole` |
| Container | `/data`, normally the `tuneconsole-data` volume |

`YT_PLAYLIST_HOME` overrides these locations. Explicit XDG paths are also
supported. See [platforms.toml](platforms.toml) for Python ABIs and runtime pins.

## Docker / Podman

Run on the same computer as your browser:

```sh
docker run -d --name tuneconsole \
  -p 127.0.0.1:8765:8765 \
  -v tuneconsole-data:/data \
  ghcr.io/reubenfirmin/tuneconsole:latest
```

Use `podman` in place of `docker` for rootless Podman. Open
<http://127.0.0.1:8765>. The image runs as UID/GID 10001. Keep the loopback port
binding: the app has no remote-user authentication. The browser and extension
stay on the host; the container needs no browser or audio device.

To update, wait for active jobs, then replace the container using the same volume:

```sh
docker pull ghcr.io/reubenfirmin/tuneconsole:latest
docker stop tuneconsole
docker rm tuneconsole
docker run -d --name tuneconsole \
  -p 127.0.0.1:8765:8765 \
  -v tuneconsole-data:/data \
  ghcr.io/reubenfirmin/tuneconsole:latest
```

Reload the app tab after startup. Removing a container preserves its volume;
removing the volume deletes the data. Back up before upgrading: older images
may not read a newer schema. Version tags are `:X.Y.Z` and `:vX.Y.Z`;
`:latest` follows stable releases. `docker logs tuneconsole` shows server output.

Local build and check:

```sh
docker build -t tuneconsole:local .
python3 packaging/container/smoke.py --engine docker --image tuneconsole:local --version 0.0.0.dev0
```

For Podman, build with `--format docker` to retain the health check, and pass
`--engine podman` to the smoke check. `--build-arg VERSION=X.Y.Z` stamps a version.
`tests/test_container_packaged.py` builds and checks disposable resources.

## Controlled dependencies

`uv.lock` pins runtime and build dependencies. Flatpak uses Python 3.13;
containers and macOS use 3.12. Builders install the locked `build`/`macos` groups
and build without isolation.

After changing dependencies:

```sh
uv lock
packaging/flatpak/generate-pip-sources.sh
python3 packaging/generate_deps.py --check
```

Review the lock and generated wheel URLs/hashes together. Both Flatpak
architectures must have compatible locked wheels.

## Release publishing

Tests run only on pull requests. A maintainer-owned PR targeting `main` builds
the next patch version, runs the packaged checks on those builds, and retains
the final wheel, DMGs, staged container digests and signed Flatpak repository.
The PR's merge tree must match the tagged commit exactly. Updates merged after
the candidate ran can change that tree.

For another version or a main commit without matching PR assets, dispatch
`prepare-release.yml` on `main` with `tag=vX.Y.Z` and wait for the candidate.
This workflow builds from its main commit and runs independently of PR checks.

Tag the selected main commit:

```sh
git fetch origin main
git tag vX.Y.Z origin/main
git push origin vX.Y.Z
```

`release.yml` locates and validates the retained artifacts, then deploys them.
It runs no builds or tests. Manual dispatch on main with `tag=vX.Y.Z` can retry
publication and optionally select a specific candidate run. Tag normalization and prerelease policy live in
[release_policy.py](release_policy.py). Prereleases do not move stable channels.

The serialized `release-production` publisher uploads a draft and SHA256
manifest, promotes container digests and the Flatpak repository, then publishes
GitHub latest last. Older releases publish immutable version downloads without
moving stable channels. Published tags accept only an identical manifest.

Before changing channels, the publisher saves `channel-recovery.json` on the
draft and as an Actions artifact. It records the old channels and candidate
identity. A confirmed failed draft restores that baseline; an ambiguous outcome
retains the checkpoint and blocks another promotion. Do not delete checkpoints
or change channels outside the coordinator. Public releases remain immutable.

### Publication recovery

Completed candidates are retained for 30 days. To reuse the original build:

```sh
python3 packaging/release_candidates.py RUN_ID vX.Y.Z EXACT_TAG_COMMIT --repository reubenfirmin/TuneConsole
gh workflow run release.yml --ref main -f tag=vX.Y.Z -f source_run=RUN_ID
```

The inspection needs an authenticated `GITHUB_TOKEN`. Choose a run with the
complete candidate set. Rerun failed preparation jobs as needed. For a legacy tag
run with all platform artifacts and a failed receipt or provenance step, dispatch
`prepare-release.yml` on main with the existing `tag` and `source_run` set to the
original tag run ID. It verifies the container images, repairs the receipts and
assembles the signed Flatpak repository from the retained platform artifacts.
Then dispatch `release.yml` using the recovery run ID to publish those assets.
Retries retain the original release clock and exact signed repository bytes once
the candidate is prepared.

Public verification checks asset hashes, both container digests, update feeds
and both signed Flatpak catalogs. A verification failure leaves public assets
intact; retry through the coordinator. The target is ten minutes, with a
15-minute limit from the original request through public verification.
`release-timing.json` and `candidate-status.json` retain timings and provenance.

Compare complete hosted records, including a cold setup:

```sh
python3 packaging/release_timing.py --compare run-1/release-timing.json run-2/release-timing.json run-3/release-timing.json --cold-run COLD_RELEASE_RUN_ID
```

Signing/publishing uses the Sparkle keys below, the configured Flatpak signing
key, Cloudflare Pages Write access and `GITHUB_TOKEN` package/release access.
Set the GHCR package to Public after its first publication and check anonymous
pulls. Keep private keys out of source.

## Serial CI shards

PR CI uses eight serial unit shards and four serial browser shards. Whole files
are partitioned using verified timing artifacts, falling back to
`.github/test-durations.json`. The coverage job checks that completed cases
equal normal collection exactly once. Browser jobs use the Playwright image
pinned to `uv.lock`. Per-shard artifacts retain JUnit, timings and failure traces.
CI reports browser elapsed time and file durations. The coverage job accepts a
complete passing matrix regardless of elapsed time.

Automatic selection compares the measured run's date, rather than its later
export date. A newer passing run can replace exported historical measurements.
When failed workflows leave the committed weights stale, explicitly refresh
them from one run's twelve downloaded shard artifacts:

```sh
python3 ci_test_shards.py evidence/ baseline --total=8 --browser-total=4 \
  --run https://github.com/reubenfirmin/TuneConsole/actions/runs/RUN_ID \
  --output .github/test-durations.json
```

The refresh requires complete, consistent, exactly-once receipts from both
suites and includes all three test phases. It retains failed case IDs and prints
the projected loads for planning. Normal CI verification requires passing
shards, and automatic timing artifacts come from successful PR workflows.

Compare complete, clean runs on the same revision using retained metrics:

```sh
python3 packaging/ci_test_metrics.py --compare \
  baseline-1/ci-test-metrics.json baseline-2/ci-test-metrics.json \
  sharded-1/ci-test-metrics.json sharded-2/ci-test-metrics.json
```

The comparison includes setup, queues and runner minutes. Projected file loads
are not measured end-to-end improvements. Local test commands and fixture rules
live in [tests/README.md](../tests/README.md).

## Linux — Flatpak

Install the Freedesktop 25.08 Platform/SDK and `flatpak-builder`, then build:

```sh
flatpak install flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
uv sync --locked --only-group build --no-install-project
uv build --wheel --no-build-isolation --python .venv/bin/python
cd packaging/flatpak
./generate-pip-sources.sh
flatpak-builder --user --install --force-clean build-dir com.tuneconsole.TuneConsole.yaml
flatpak run com.tuneconsole.TuneConsole
```

Keep one application wheel in `dist/`; rebuild it after code changes and regenerate
pip sources after dependency changes. The sandbox grants network access and
uses the OpenURI portal for the host browser. It grants no host filesystem access.

Run real installation/update checks explicitly:

```sh
uv run pytest --run-flatpak-build -m '' tests/test_flatpak_packaged.py
```

These use temporary installations; update checks also need Playwright Chromium,
GPG, D-Bus and the host Flatpak portal/session/permission-store executables.
The maintainer owns runtime upgrades; pins and review dates are in
[platforms.toml](platforms.toml). Flathub submission is deferred; see its
[reopening requirements](flathub/README.md).

### Linux updates

The Home card installs from `https://repo.tuneconsole.com/`, then offers a
restart after active work finishes. Same-runtime updates reconnect the existing
tab. Runtime changes may require closing all app tabs and launching again.
Closing all tabs normally stops a packaged Linux launch; source launches stop
with Ctrl-C.

For an old bundle without an application origin, close the app and reconnect
it in the same installation scope (`--user` below):

```sh
flatpak remote-add --user --if-not-exists tuneconsole https://repo.tuneconsole.com/tuneconsole.flatpakrepo
flatpak install --user --reinstall tuneconsole com.tuneconsole.TuneConsole
flatpak info --user --show-origin com.tuneconsole.TuneConsole
flatpak update --user com.tuneconsole.TuneConsole
```

Reinstallation preserves private data. Do not uninstall with `--delete-data`.
An opt-in published-repository check uses `tests/bench_public_flatpak.py` with
`TUNECONSOLE_PUBLIC_RELEASE_MANIFEST` set to the trusted retained manifest.

## macOS — PyInstaller `.app` + `.dmg`

```sh
cd packaging/macos
export SPARKLE_PUBLIC_ED_KEY="<your Sparkle public key>"
./build.sh
./smoke.sh
```

The local build embeds Python and dependencies and defaults to ad-hoc signing.
Release downloads use Developer ID signing and Apple notarization.
`TuneConsole-macos.dmg` is the Apple Silicon download. Intel Macs use
`TuneConsole-macos-x86_64.dmg`. Open the download, drag TuneConsole to
**Applications**, eject the disk image, and open TuneConsole from Applications.
macOS may show its usual confirmation for an app downloaded from the internet.

The app menu provides **Check for Updates…** and **Quit TuneConsole**. Sparkle
verifies Ed25519 updates and relaunches after active jobs drain. Each architecture
has its own appcast. For a manual upgrade from an older ad-hoc build, quit
TuneConsole and replace `TuneConsole.app` in Applications with the signed copy.
Keep `~/Library/Application Support/TuneConsole` to retain your library,
settings and browser pairing.

### Set up Apple signing

1. On a Mac, sign in with the maintainer’s own Apple Account and enable
   two-factor authentication. [Enroll in the Apple Developer Program](https://developer.apple.com/programs/enroll/).
   Apple lists US$99 per membership year, with regional pricing. Individual
   enrollment uses your legal name. An organization enrolls under its legal
   entity name and generally needs a D‑U‑N‑S Number and someone authorized to
   bind the organization.
2. In Keychain Access, choose **Certificate Assistant > Request a Certificate
   From a Certificate Authority** and save the request to disk. In the Apple
   developer account, create a [Developer ID Application certificate](https://developer.apple.com/help/account/certificates/create-developer-id-certificates)
   using that request. Developer ID Installer applies to `.pkg` installers.
3. Install the certificate on the Mac that created the request. In Keychain
   Access, export the certificate and corresponding private key together as a
   password-protected `.p12`. Get its full identity with
   `security find-identity -v -p codesigning` and its Team ID from Apple Developer
   membership details. Create an [app-specific password](https://support.apple.com/en-us/102654)
   for notarization in the Apple Account used with that team.
4. Add these repository Actions values. Add secrets through GitHub and store
   the private key and its password separately in your credential manager.

   | Actions setting | Type | Value |
   | --- | --- | --- |
   | `MACOS_TEAM_ID` | Variable | Apple Developer Team ID |
   | `MACOS_SIGNING_IDENTITY` | Variable | Full identity, `Developer ID Application: Name (TEAMID)` |
   | `MACOS_CERTIFICATE_P12` | Secret | Base64 of the protected export, from `base64 -i /path/to/certificate.p12` on macOS |
   | `MACOS_CERTIFICATE_PASSWORD` | Secret | Password used when exporting the `.p12` |
   | `MACOS_NOTARY_APPLE_ID` | Secret | Apple Account email for notarization |
   | `MACOS_NOTARY_APP_PASSWORD` | Secret | App-specific password for that account |
   | `SPARKLE_PUBLIC_ED_KEY` | Variable | Existing Sparkle Ed25519 public key |
   | `SPARKLE_PRIVATE_ED_KEY` | Secret | Existing Sparkle Ed25519 private key |

For a new setup, generate Sparkle keys once using the pinned Sparkle tools:

```sh
packaging/macos/.sparkle/bin/generate_keys
packaging/macos/.sparkle/bin/generate_keys -p
```

Export the Keychain private key with `generate_keys -x <secure-file>`. Keep
the Sparkle key stable across releases, including the first Developer ID release.

### Prepare and verify a release

The trusted release-candidate workflow builds on Apple Silicon and Intel runners.
Each runner uses a temporary keychain, signs the app and its dependencies
and signs the DMG. It submits the DMG to Apple, requires an Accepted result,
staples and validates the ticket on the DMG, then verifies Gatekeeper acceptance
for the image and its embedded app. Sparkle signs the exact stapled DMG.
Promotion checks the notarization receipts and reuses the prepared downloads.
[Apple distribution packaging](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
and [GitHub runner certificate setup](https://docs.github.com/en/actions/how-tos/deploy/deploy-to-third-party-platforms/sign-xcode-applications)
describe these steps.

The workflow logs the submission ID before waiting up to six minutes for Apple.
Look in the `macos-notary-logs-*` artifacts for Apple’s inspection log. For a
rejection, fix the reported files before rebuilding. For a timeout, use
`xcrun notarytool info <submission-id> --keychain-profile <local-profile>` on a
Mac with the same Apple team configured. Retry a failed candidate job once Apple
is reachable and any signing issue is resolved. If candidates are complete
and only publication failed, retry publication using the original Actions run.

For initial verification, use the available Mac to download the DMG through
a browser and follow the installation steps above with quarantine enabled.
Record the macOS version and CPU architecture. Confirm that TuneConsole opens
the browser and stops the local server when quit. Test Sparkle between two
successive signed releases and confirm the library and browser pairing survive.
Try the update from an ad-hoc installation as well and record whether manual
replacement is needed. The runner’s signature checks and PR smoke tests are
separate from these checks on an installed copy.
