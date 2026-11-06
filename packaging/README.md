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

Tests run only on pull requests. After the source checks pass, PR workflows
build both architectures and run packaged checks. Main and release workflows
build and publish without unit/browser suites, package smoke tests, synthetic
publisher rehearsals or a test-history gate.

Tag the selected main commit:

```sh
git fetch origin main
git tag vX.Y.Z origin/main
git push origin vX.Y.Z
```

`release.yml` builds the wheel, container images, macOS DMGs/feeds and signed
Flatpak bundles/repository from that commit. It retains hashes and exact build
artifacts before publication. Manual dispatch on main with `tag=vX.Y.Z` uses
the same flow. Tag normalization and prerelease policy live in
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

The inspection needs an authenticated `GITHUB_TOKEN`. Choose the original build
run, including a failed publisher run with all candidates uploaded. Wrong
revisions, missing/expired artifacts or ambiguous duplicates block reuse. Rerun
failed build jobs for missing candidates; expired candidates need rebuilding.
Retries retain the original release clock and exact signed repository bytes.

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

PR CI uses four serial unit shards and four serial browser shards. Whole files
are partitioned using verified timing artifacts, falling back to
`.github/test-durations.json`. The coverage job checks that completed cases
equal normal collection exactly once. Browser jobs use the Playwright image
pinned to `uv.lock`. Per-shard artifacts retain JUnit, timings and failure traces.

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

The build embeds Python and dependencies. Releases provide separate arm64 and
x86-64 DMGs; `TuneConsole-macos.dmg` aliases arm64. Install in Applications.
The ad-hoc signed app may require **Open Anyway** in System Settings,
**Privacy & Security** on first launch. Do not disable Gatekeeper globally.

The app menu provides **Check for Updates…** and **Quit TuneConsole**. Sparkle
verifies Ed25519 updates and relaunches after active jobs drain, preserving data
and the existing browser tab. Each architecture has its own signed appcast.
Sparkle's pinned framework and helpers are copied intact; the app is not
Developer ID signed or notarized.

Generate signing keys once using the downloaded Sparkle tools:

```sh
packaging/macos/.sparkle/bin/generate_keys
packaging/macos/.sparkle/bin/generate_keys -p
```

Set Actions variable `SPARKLE_PUBLIC_ED_KEY` and export it for local builds.
Export the Keychain private key with `generate_keys -x <secure-file>` and save
it as Actions secret `SPARKLE_PRIVATE_ED_KEY`. Keep the key stable across
releases. Verify two successive builds on a Mac, including cancelled downloads,
invalid signatures, active-work drain and relaunch without a second tab.
