# Packaging

Build TuneConsole (package `yt-playlist`) as a **Flatpak** (Linux), a **self-contained `.app` +
`.dmg`** (macOS), or a **Docker / Podman image** (Linux x86-64 and ARM64).
The desktop launchers start the local server and open it in your browser automatically
(`yt-playlist --open`; the Flatpak opens the host browser via the OpenURI portal).

> **You also need the TuneConsole browser extension.** The app doesn't talk to YouTube Music
> directly — a lightweight Chrome/Chromium extension bridges your signed-in YouTube Music tab to the
> local app over `127.0.0.1:8765`, so your session never leaves the browser. Install it from the
> Chrome Web Store (or load `../extension/` unpacked) and keep a `music.youtube.com` tab open.
> Without it the app still runs, it just has nothing to sync.

Where the app keeps its data:

- **Linux/Flatpak:** `~/.var/app/com.tuneconsole.TuneConsole/{config,data}` — its private sandbox dir.
- **macOS:** `~/Library/Application Support/TuneConsole` for config/data and
  `~/Library/Logs/TuneConsole` for logs.
- **Docker / Podman:** `/data` in the container, backed by the `tuneconsole-data` named volume.

`paths.py` honours `$YT_PLAYLIST_HOME` first and explicit `$XDG_CONFIG_HOME` / `$XDG_DATA_HOME`
second. Otherwise it uses the platform-native paths above on macOS and the usual XDG fallbacks on
Linux.

---

## Docker / Podman

Run the container engine on the same computer as your browser. The extension connects to
`127.0.0.1:8765`; a remote server or NAS needs a local tunnel and is not the default setup.
On macOS or Windows, Docker Desktop or a Podman machine supplies the Linux runtime; the
browser and extension stay on the host. CI tests the Linux images on native x86-64 and ARM64
runners; desktop VM integrations need verification on those hosts.

### Install and run

```sh
docker run -d --name tuneconsole \
  -p 127.0.0.1:8765:8765 \
  -v tuneconsole-data:/data \
  ghcr.io/reubenfirmin/tuneconsole:latest
```

Replace `docker` with `podman` throughout these instructions for rootless Podman.
Open <http://127.0.0.1:8765> and install the browser extension as usual. The container keeps
running when you close the UI; `docker stop tuneconsole` stops it and
`docker start tuneconsole` starts it again. Use `docker logs tuneconsole` for server output.

The image runs as UID/GID 10001, sets `YT_PLAYLIST_HOME=/data`, and keeps config, SQLite,
logs, and backups in that directory. The engine creates the named volume with suitable
ownership on first use. Removing the container preserves the volume. Removing the volume
deletes the app's data. Existing desktop installations have separate data; this starts a new
library unless you deliberately migrate your config/data while both instances are stopped.

The server listens on `0.0.0.0` inside the container; the published host port binds to
`127.0.0.1`. Keep that host binding: this is a local application without remote-user
authentication. The browser bridge retains its extension-origin checks. No browser, audio
device, privileged mode, or host-network access is needed inside the container.

### Update

Wait for syncs, imports, and model jobs to finish, then pull and replace the container:

```sh
docker pull ghcr.io/reubenfirmin/tuneconsole:latest
docker stop tuneconsole
docker rm tuneconsole
docker run -d --name tuneconsole \
  -p 127.0.0.1:8765:8765 \
  -v tuneconsole-data:/data \
  ghcr.io/reubenfirmin/tuneconsole:latest
```

Use the same volume each time. Reload the existing browser tab after the new container starts.
The app's update card links to these instructions on the website. Versioned tags are also
published (`:X.Y.Z` and `:vX.Y.Z`); `:latest` follows stable releases. Back up your data before
upgrading; older images may not understand a newer database schema.

### Build and verify locally

```sh
docker build -t tuneconsole:local .
python3 packaging/container/smoke.py --engine docker --image tuneconsole:local --version 0.0.0.dev0
```

Use `podman build --format docker -t tuneconsole:local .` and `--engine podman` to verify Podman.
For a disposable build and smoke check under the shared test lock, run
`TUNECONSOLE_TEST_CONTAINER_ENGINE=podman uv run pytest tests/test_container_packaged.py`
(or select `docker`). The check removes its own image, container and volume afterwards.
The Docker image format preserves the health check, which Podman's default OCI build format omits.
The multi-stage Dockerfile installs
runtime dependencies from `uv.lock`, builds a wheel, and copies only the installed environment
into the runtime image. Git history and local app data are excluded from the build context.
A local build defaults to version `0.0.0.dev0`; `--build-arg VERSION=X.Y.Z` stamps a release
version without needing Git inside the image.

### Controlled dependencies

`uv.lock` controls runtime versions on all shipped platforms. `packaging/platforms.toml`
records each Python ABI and architecture. Flatpak uses Python 3.13; containers and macOS use
3.12. Environment markers retain Linux-only D-Bus support and omit Windows-only packages;
uvicorn's `standard` extras remain included. Platform wheels differ, while compatible
runtime package versions come from the same resolution.

The `build` and `macos` dependency groups pin the packaging tools in that lock. Wheel,
container, and macOS builds install those groups before building without isolation, so the
build backend cannot resolve another set of tools. macOS requires uv and Python 3.12.

For an upgrade, update `pyproject.toml` as needed, run `uv lock` (or `uv lock --upgrade-package
NAME` for a deliberate package upgrade), then `packaging/flatpak/generate-pip-sources.sh`.
Review the lock and generated wheel URLs/hashes together. CI uses `uv sync --locked` and
`packaging/generate_deps.py --check` to reject stale inputs. The generator consumes uv's
marker-aware export, selects locked wheels for both Flatpak architectures, and fails if a
compatible wheel is absent; it downloads no generator and repeats no dependency list.

The smoke test uses its own disposable container, volume, and random loopback port. It checks
the UI/static assets, installed version, non-root storage, extension WebSocket handshake,
rejection of untrusted origins, persisted database/backups across replacement, and clean
shutdown. It removes only its own test resources afterward.

### Release publishing

`.github/workflows/container.yml` builds and smoke-tests both architectures on pushes and pull
requests. The release coordinator calls it for tags, stages the exact tested image digests,
and publishes their multi-platform manifest after all release checks pass. Publishing uses the repository's
`GITHUB_TOKEN` with `packages: write`; no registry password or additional secret is needed.
The image carries source, revision, version, and license labels.

After the first tagged publication, set the GitHub container package's visibility to **Public**
in its package settings and verify an unauthenticated pull before deploying the website's
container install option. GHCR creates new packages as private even for public repositories.
Prerelease tags get their versioned images without moving `latest`.

All packaging workflows use `packaging/release_policy.py`. Supported release tags are `vX.Y.Z`,
PEP 440 prereleases (`vX.Y.ZrcN`, `vX.Y.ZbN`, `vX.Y.ZaN`, `vX.Y.Z.devN`) and their
`-rc.N`, `-beta.N`, `-alpha.N`, `-dev.N` equivalents. Package versions normalize to PEP 440;
unsupported tags fail before publication. Prereleases and development builds are explicitly
GitHub prereleases, never become GitHub/container `latest`, and never deploy the stable
Flatpak repository. There is currently no opt-in Flatpak prerelease update channel.

`release.yml` owns tag publication. It waits for source CI, the wheel, signed Flatpak bundles
for both architectures, both macOS DMGs and signed feeds, and both container smoke checks.
Build jobs upload Actions candidates and never change stable channels. One serialized
promotion job validates versions, feeds, digests and the complete artifact set, uploads a
draft GitHub release with a SHA256 manifest, and promotes the tested container digests.
It snapshots the existing Flatpak deployment and container `latest`, deploys the candidate
repository, moves container `latest`, then publishes GitHub latest last. Older completed
runs publish their immutable version downloads without moving any stable channel.

A failed upload leaves a draft. Failed channel promotion attempts restore the prior Pages
deployment and container digest before failing; errors restoring either channel are explicit.
The services do not provide a shared atomic transaction, and a forced runner termination
or service outage can require operator recovery. Keep the preceding Pages deployment and
container digest available. The first release has no prior channels to restore. Retry a
failed workflow with the same candidate artifacts; a published tag accepts only an identical
manifest and is otherwise immutable. Do not publish these candidates manually outside the
coordinator, since its shared concurrency group owns ordering and rollback.

---

## Linux — Flatpak

The sandbox is deliberately tight: the only host permission is `--share=network`. No host filesystem
access is granted; config, `state.db`, and backups live in the per-app directory (Flatpak redirects
`XDG_*_HOME` there). Opening the browser uses the OpenURI portal. Downloaded share `.txt` files are
written by your browser, which runs outside the sandbox.

### Prerequisites
```sh
flatpak install flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
# flatpak-builder: e.g. `sudo dnf install flatpak-builder` or `flatpak install flathub org.flatpak.Builder`
```

### Build
```sh
# from the repo root
uv sync --locked --only-group build --no-install-project
uv build --wheel --no-build-isolation --python .venv/bin/python

cd packaging/flatpak
./generate-pip-sources.sh                          # -> locked Python 3.13 wheels for both architectures

flatpak-builder --user --install --force-clean build-dir com.tuneconsole.TuneConsole.yaml
```

### Run
```sh
flatpak run com.tuneconsole.TuneConsole            # starts the server + opens the browser
flatpak run com.tuneconsole.TuneConsole --port 9000   # extra args pass through
```

Regenerate `python3-requirements.json` whenever `[project.dependencies]` changes, and rebuild the
wheel (using that same build command) whenever the app code changes, before re-running
`flatpak-builder`. Keep exactly one application wheel in `dist/`.

To check a real local build without changing your installed app, run
`uv run pytest --run-flatpak-build -m '' tests/test_flatpak_packaged.py`.
The second check also needs Chromium from Playwright, GPG, D-Bus, and the host's
`flatpak-portal`, `flatpak-session-helper`, and `xdg-permission-store` executables. It builds
two real application versions, installs a signed bundle into a clean temporary user
installation, and uses the Home card to update and restart. Its private session bus and
permission store pregrant update consent without touching desktop settings. It verifies
the new running commit, the same tab reconnecting, and preserved library, config, and
browser pairing. It covers an automatic restart on 25.08/Python 3.13 and the safe fresh-launch
fallback when migrating from 24.08/Python 3.12. No application repository or runtime is installed into your user account.
It builds a wheel and the offline Flatpak inputs in temporary directories, then checks the
supported runtime, startup, extension WebSocket origins, logs, and persisted state. The release
workflow also installs each architecture's signed bundle and runs `flatpak/smoke.py` against it.

Freedesktop 25.08 is supported through September 2027 according to the
[upstream release timeline](https://freedesktop-sdk.gitlab.io/documentation/wiki/Releases.html).
The repository release maintainer owns runtime support. Review the next runtime migration
by June 2027 and complete it before September; dates are recorded in `platforms.toml`.
Changing the runtime also requires changing its Python ABI, regenerating both wheel sets,
and running the packaged installation/upgrade, startup, browser bridge and updater checks.
The retired Flathub submission has no maintained manifest to migrate.

### Notes
- The repo ships an MIT `LICENSE` at its root, matching `project_license` in the metainfo.
- **Distribution ownership:** the repository maintainer owns the signed, self-hosted Flatpak
  repository and release bundles. Flathub application submission is deferred; its obsolete
  manifest and duplicated dependency snapshot have been removed. See the
  [reopening requirements](flathub/README.md) before starting a new submission.

---

### Linux updates

The Home update card offers **Update now**. Flatpak may show a desktop permission dialog on the
first update; the card then shows download/install progress and **Restart to update**. Restart waits
for sync, imports, and registered background jobs to finish, launches the installed version in a new
sandbox, and reconnects the same browser tab. Updating doesn't change the app's sandbox permissions.

If an update requires additional permissions, or self-updates are denied/unavailable, the card links
to the desktop software manager. Updates installed independently by the software manager are also
detected, so the running app can offer a restart. Published updates must be in the installed app's
configured Flatpak repository; the GitHub release notice alone doesn't install an update.

When a runtime change prevents the installed version from starting in the current sandbox,
TuneConsole keeps the current server available and asks you to close all TuneConsole tabs,
wait for the application to exit, and open it again from your applications menu. The Flatpak Spawn portal retains the
caller's runtime branch even when launching the latest application commit; a fresh desktop
launch selects the new runtime. The app checks that the replacement can start before stopping
the current server. See the [portal implementation](https://github.com/flatpak/flatpak/blob/main/portal/flatpak-portal.c).

Release bundles embed the application repository URL (`https://repo.tuneconsole.com/`) and
its signing key, separately from the Flathub runtime repository. Installing a new bundle
therefore configures a verified application origin automatically. See the
[Flatpak bundle options](https://docs.flatpak.org/en/latest/flatpak-command-reference.html#flatpak-build-bundle).

For an older bundle installed without an update origin, close TuneConsole and reconnect it
to the signed repository in the **same installation scope** (these commands use `--user`;
use `--system` for a system installation):

```sh
flatpak remote-add --user --if-not-exists tuneconsole https://repo.tuneconsole.com/tuneconsole.flatpakrepo
flatpak install --user --reinstall tuneconsole com.tuneconsole.TuneConsole
flatpak info --user --show-origin com.tuneconsole.TuneConsole
flatpak update --user com.tuneconsole.TuneConsole
```

Reinstallation preserves the app's private config and data. Do not uninstall with
`--delete-data`. The release check must install the bundle in a clean user installation,
inspect its origin and GPG verification, serve a newer signed commit from that origin,
then update and restart through the Home card. Check that the version changes while the
existing library and browser pairing remain available.

To stop a packaged Linux launch normally, close all TuneConsole tabs and wait a few seconds. A
development launch (`uv run yt-playlist`) stays running until Ctrl-C in its terminal.

---

## macOS — PyInstaller `.app` + `.dmg`

```sh
cd packaging/macos
export SPARKLE_PUBLIC_ED_KEY="<your Sparkle public key>"
./build.sh
```

Produces `dist/TuneConsole.app` and `dist/TuneConsole-0.1.1.dmg`. Python and all dependencies are
embedded — nothing is required on the target Mac. The DMG contains an Applications shortcut for
normal drag-to-install behavior.

The release workflow builds two independent packages:

- `TuneConsole-macos-arm64.dmg` for Apple Silicon Macs.
- `TuneConsole-macos-x86_64.dmg` for Intel Macs.

`TuneConsole-macos.dmg` remains an Apple Silicon alias for the website's stable download URL.

### First launch
The macOS bundle is ad-hoc signed without an Apple Developer ID certificate. Gatekeeper therefore
blocks an ordinary double-click the first time:

1. Open the downloaded DMG and drag TuneConsole to Applications.
2. In Applications, Control-click or right-click TuneConsole and choose **Open**.
3. Confirm **Open** in the warning dialog.

If macOS does not offer Open there, attempt one normal launch, then use System Settings → Privacy &
Security → **Open Anyway**. Do not disable Gatekeeper globally. Subsequent launches open normally.

The server opens your browser; a small Cocoa host provides **Check for Updates…** and **Quit
TuneConsole** in the app menu. Dock → **Quit** also works. Quitting waits for active syncs, imports,
and registered background jobs before shutting down the server.

### Custom icon (optional)
`build.sh` calls `make-icns.sh`, which converts the 1024px `TuneConsole.png` export to
`TuneConsole.icns` using macOS's built-in `sips` and `iconutil`. Without them the app gets PyInstaller's default
icon — harmless.

### Verify the packaged app

```sh
./smoke.sh
```

This launches the frozen executable without opening a browser, waits for its HTTP server, checks a
live endpoint, confirms its database/log files, and verifies the DMG. CI runs it on both CPU builds.

### Updates

The Home card's **Update now** button opens Sparkle's native update window. Sparkle handles the
download, Ed25519 signature verification, replacement, and **Install and Relaunch**. It also checks
automatically (daily by default); users can change automatic-update preferences in its window.
The app waits for active syncs/imports/jobs, shuts down its server, and reconnects the existing
browser tab at the same address after relaunch. User data remains in Application Support.

The bundle includes Sparkle 2.10.0, fetched from its upstream release with a pinned SHA-256. Its
signed framework and helper tools are copied intact. The app remains ad-hoc signed rather than
Developer ID signed/notarized, so the first-install Gatekeeper instructions above still apply.
Install in Applications before updating; Sparkle handles read-only disk images and installation
authorization through its native UI.

Each architecture has its own feed (`appcast-macos-arm64.xml` / `appcast-macos-x86_64.xml`) attached
to GitHub Releases, pointing to immutable DMG URLs on that tag. The release workflow uses Sparkle's
`generate_appcast` to sign the DMG and generate the feed. The private key is passed on stdin and
never bundled. A missing or invalid signing configuration fails the build instead of publishing
unsigned updates.

One-time release setup, on a Mac with the downloaded Sparkle tools:

```sh
# build.sh downloads .sparkle/ before checking the public-key setting.
packaging/macos/.sparkle/bin/generate_keys
packaging/macos/.sparkle/bin/generate_keys -p
```

Store the printed public key as the repository Actions variable `SPARKLE_PUBLIC_ED_KEY`, and export
the same value for local builds. Back up the private key from Keychain using
`generate_keys -x <secure-file>` and store that exported value as the Actions secret
`SPARKLE_PRIVATE_ED_KEY`. Keep
the key stable across releases. Do not commit private keys. These are separate from Apple Developer
ID certificates and the Flatpak repository signing key.

Existing macOS installs need one manual DMG replacement to acquire Sparkle. Test two successive
tagged builds on a Mac before shipping broadly: update from Applications, cancel/retry a download,
reject an invalid signature, wait for active work, and verify relaunch without a second tab.
