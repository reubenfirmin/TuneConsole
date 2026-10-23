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

Ordinary CI runs once for each PR update (including fork PRs), validating GitHub's synthetic
merge revision, and once for each push to `main`, validating the landed revision. Feature-branch
pushes do not duplicate PR checks. Manual and reusable runs remain available. Superseded PR
and main checks are cancelled within their event/ref; tag release calls cannot cancel releases.
Tag pushes start only the release coordinator, which requires an already successful `Tests`
run for the exact tagged commit (a main push or explicit manual run). A missing, running,
failed or cancelled CI result blocks promotion; rerun the release after CI succeeds.
Releases never run the source suite again.

After both source suites and packaged-resolution checks pass, `ci.yml` calls the container,
macOS and Flatpak build workflows. Both supported architectures build the actual locked
packages and run their packaged startup checks, including the Flatpak extension bridge.
Ordinary/fork CI needs no signing or publication secrets. Flatpak CI installs an unsigned
local candidate. Final release preparation uses the same build/smoke owners with the
prospective release version, signed bundles and architecture-specific update feeds.

Prepare final candidates **before** creating the release tag:

```sh
gh workflow run ci.yml --ref main -f release_tag=vX.Y.Z
```

This trusted manual run normalizes the final version and bakes it into every package
before packaged startup checks. If ordinary source CI already passed for this exact
commit, it reuses that result and runs only the later packaging stage; otherwise the
source checks run first. A different revision or PR merge result cannot admit reuse.
CI exports immutable candidate artifacts, exact hashes, commit/tag/version/run identity,
both architectures for every platform, and the final merged signed Flatpak repository.
After this CI completes, tag its **exact tested commit** with the requested tag. No
application payload or signed repository bytes change during release promotion.

`.github/workflows/container.yml` is reusable and also supports intentional manual runs.
CI stages the exact tested image digests, and the release coordinator publishes their
multi-platform manifest after validating candidate provenance. Publishing uses the repository's
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

`release.yml` owns tag publication. It retrieves the successful CI run's wheel, signed
Flatpak bundles/repository, macOS DMGs/feeds and container digests for the exact revision
and requested version. It has no platform build or source test jobs. CI build jobs
upload Actions candidates and never change stable channels. One serialized
promotion job validates versions, feeds, digests and the complete artifact set, uploads a
draft GitHub release with a SHA256 manifest, and promotes the tested container digests.
It snapshots the existing Flatpak deployment and container `latest`, deploys the candidate
repository, moves container `latest`, then publishes GitHub latest last. Older completed
runs publish their immutable version downloads without moving any stable channel.

A failed upload leaves a draft. Before stable channels change, the coordinator uploads and
reads back `channel-recovery.json` on that draft, recording the previous Pages deployment,
container digest, latest release and exact candidate manifest identity. The workflow also
retains this evidence as an Actions artifact. A failed promotion restores the baseline only
when GitHub confirms non-publication. If publication or its manifest cannot be read, the
outcome is explicitly unresolved: channels remain in place and the checkpoint is retained.
Rollback failures also retain the checkpoint. Another tag cannot promote while a draft has
an unresolved checkpoint.

Retry the same tag and identical candidates through the coordinator when services recover.
A confirmed public release retains its channels; a confirmed draft restores its original
baseline before a fresh attempt. A changed GitHub latest blocks stale automatic rollback.
Successful publication keeps the checkpoint as immutable audit evidence; successful rollback
removes it from the draft. The first release has no prior channels to restore. The services
do not provide a shared atomic transaction, so a forced runner termination can require this
reconciliation. Do not delete a checkpoint or publish candidates manually outside the shared
`release-production` lock. A published tag accepts only an identical manifest.

After source checks on trusted `main` runs, CI rehearses the real GitHub draft and
asset API with a unique `publisher-contract-ci-*` draft. The fixture uploads are
small, include a partial upload followed by complete staging and an idempotent
repeat, and are removed in both a `finally` block and an `always()` workflow step.
PRs do not receive this write token. No rehearsal publishes a release, creates a
Git tag, or touches an update channel. Focused local contract tests also cover
published tag lookup, authenticated paginated draft lookup, upload metadata,
interrupted uploads, HTTP errors and unreachable services.

Release preflight repeats this isolated lifecycle before platform builds, proves
GHCR push permission with a cancelled empty blob upload (no image tags), requests
a Cloudflare Pages upload token (requires Pages Write), and checks the recoverable
stable baseline. Failures identify the service without dumping authenticated
responses. An outage can prevent draft cleanup; retry the cleanup step using the
exact `REHEARSAL_TAG` shown in that run before abandoning it.

After publication, focused anonymous checks download every asset and verify its
SHA256 against the candidate, check both versioned container architectures and
stable `latest` against the tested digests, and fetch the Flatpak summary/signature
from `https://repo.tuneconsole.com/`. Flatpak then verifies the signed remote in
isolated user state and confirms both architectures and the release version.
Repository assembly uses one signed summary rather than architecture-filtered summary
indexes, allowing a fresh verifier to fetch both AppStream branches. After refreshing
each branch, verification checks its catalog's newest release and architecture-specific
bundle; `remote-ls` alone can omit a non-host architecture's display version.
The workflow summary records each verified download and channel. A failure here
leaves the published release immutable; retry verification with the same candidates
through the coordinator instead of deleting or replacing public assets.

Publication recovery does not rebuild successful candidates. The release coordinator
has a `workflow_dispatch` form with `tag` and `source_run`: choose the original tag
build run, not a later publication-only retry. New releases use the successful final-candidate
CI run; retained original tag-build runs remain supported during the transition. Before dispatch, inspect availability
with an authenticated token in `GITHUB_TOKEN`:

```sh
python3 packaging/release_candidates.py RUN_ID vX.Y.Z EXACT_TAG_COMMIT --repository reubenfirmin/TuneConsole
gh workflow run release.yml --ref main -f tag=vX.Y.Z -f source_run=RUN_ID
```

The inspection requires the exact tag and SHA from trusted CI (nine artifact groups,
including provenance and the assembled signed repository), or the original release
workflow (seven platform groups), and lists byte sizes and expiry state. A failed
legacy publisher run is an acceptable source when every candidate was uploaded successfully.
An active unrelated run, wrong revision/tag, failed/skipped platform with missing
artifacts, ambiguous duplicates or expired artifacts blocks promotion. Rerun only
failed source jobs to complete missing candidates; expired candidates require rebuilding
the affected platform. Artifact IDs, not mutable names alone, select the downloads.
Candidates remain available for 30 days. Publication retries skip every build job
and use the same production lock, immutable manifest and channel reconciliation.
CI retains the exact assembled signed repository, so new retries never regenerate
its timestamped summary. For a legacy confirmed draft, recovery validates the saved
checkpoint against the remote draft manifest and restores the original stable
baseline before validating or staging newly assembled bytes. Public releases always
require the original manifest; a mismatched retry cannot modify their assets.

Setup has independent budgets: apt operations use 15-second network timeouts and
two transient retries within bounded tool-install steps; Flatpak runtime/source
downloads each allow two 120-second attempts; macOS dependency preparation allows
two 240-second attempts, with bounded Sparkle downloads and uv requests. Application
builds have separate step limits and keep architecture-specific startup checks.
Trusted uv, Sparkle archive and Flatpak runtime/source caches reduce repeated setup;
lockfiles, Sparkle SHA256 and signed runtime metadata remain authoritative.
Setup timing JSON is uploaded even after failure, and the final run summary preserves
candidate availability, completed stages and a publication-only recovery command.
Publication commands have a 180-second bound. A timed-out channel command keeps
its saved baseline for reconciliation instead of assuming it did not commit.

The release target is 10 minutes, with a 15-minute limit measured from completion
of the selected CI run through successful **public** endpoint verification. The
durable `release-timing.json` includes runner queue delays, retrieval, tools, channel
promotion and verification, the exact release attempt, source CI link and candidate
sizes. `candidate-status.json` binds those sizes to the tag, revision and CI clock.
Publication retries keep that source clock. For legacy builds, the clock comes from
the latest successful exact-revision CI that finished before the original release
started; a failed release's completion cannot restart it. The final
summary fails the timing check if publication is incomplete or exceeds 900 seconds.
It does not roll back an already public release to make a timing result pass.

| Critical path stage | Normal target | Bound / recovery |
| --- | --- | --- |
| Version/source checks and candidate selection | 60s | Each read job has a 1–2 minute limit; retry reads |
| Service preflight | 30s | 2 minutes; service and permission reported |
| Candidate retrieval and publication tools | 90s | Bounded download/setup steps; retain CI artifacts |
| Restore CI's signed repository | 15s | 2 minutes; exact archive retained |
| Draft upload and channel promotion | 120s | 180s shared command/API budget; retain checkpoint on ambiguity |
| Anonymous downloads, feeds and registry verification | 90s | 2 minutes; published assets remain immutable |
| Runner queues and variance | 195s | Included in measured elapsed time |

Before accepting the duration target, collect three hosted runs of this handoff,
including a cold setup, with source CI/run links, artifact sizes and `release-timing.json`.
Those rehearsals are **pending**; local fixture tests do not establish hosted duration.
Retain each run's `release-status-<attempt>` artifact, including `release-timing.json`.
After checking the cold runner's setup logs, validate the three records together:

```sh
python3 packaging/release_timing.py --compare run-1/release-timing.json run-2/release-timing.json run-3/release-timing.json --cold-run COLD_RELEASE_RUN_ID
```

The resulting `release-rehearsals.json` records source/release links, revisions,
sizes and timings. It rejects duplicate runs, incomplete architecture artifacts,
missing provenance and late or failed public verification. Cold setup is identified
by the reviewer from runner logs; the checker also requires that run's successful
tool-install stage. Use representative publications that exercise the complete
handoff rather than repeated verification of an already public release.

The earlier [publication-only recovery run](https://github.com/reubenfirmin/TuneConsole/actions/runs/39986299269)
completed in 100 seconds (16:23:02–16:24:42 UTC on 2026-10-23), showing the cost of
promotion without rebuilding, but it did not exercise this new handoff or its public
verification checks. The [original source run](https://github.com/reubenfirmin/TuneConsole/actions/runs/39982084139)
retained 536,621,081 bytes across the seven platform artifact groups. Its first x86_64
setup spent 533 seconds in tool installation before cancellation; these are baseline
observations, not successful rehearsals of the new 15-minute path.

---

## Serial CI shards

Ordinary CI uses two unit runners and two browser runners. Each owns one serial
pytest process and the usual machine lock. The `checks` job remains the admission
gate for every packaging workflow; failed, cancelled or unexpectedly skipped matrix
jobs or the coverage check fail admission. Python 3.12 and Node remain on the unit
runners, with the separate packaged-resolution check on unit shard 1 only.

The planner pins one immutable timing artifact from successful main-branch CI for
all four runners. Whole files are assigned deterministically by measured duration;
new files use measured median per-case estimates and join the partition automatically.
The first run without timing history uses clearly labelled bootstrap estimates.
Every shard records normal collection before partitioning, including parametrized
case IDs. The coverage job compares both collections, source revisions and file plans,
then requires the union of completed cases to equal normal collection exactly once.
It does not collect or execute another full test suite.

Per-shard artifacts retain JUnit, setup/call/teardown timings and browser failure
traces for 30 days. The combined timing artifact feeds subsequent partitions.
`ci-test-metrics.json` records queue/setup/execution time, test runner minutes and
failed-job links. It measures planning through the completed `checks` gate; the
separate reporting job and downstream packaging are excluded from that source-test
budget. Compare repeated clean runs on the same revision, including cold setup and
repeated session-fixture cost, before increasing beyond two shards per suite.

For the hosted comparison, manually run **Tests** at the same fixed revision at
least twice with `test_shards=1` and twice with `test_shards=2`, waiting for each run
to finish before starting the next. Leave `release_tag` empty so prior checks are
not reused. The default remains two shards for pushes,
pull requests and reusable calls. The one-runner control runs the whole suite
serially and proves coverage using the same reports and gate. Test runners restore
no uv cache, so dependency and browser installation costs remain in the measurement.
Download each run's `source-test-run-metrics-<attempt>` artifact into a separate
directory, then compare the retained JSON without executing tests:

```sh
python3 packaging/ci_test_metrics.py --compare \
  baseline-1/ci-test-metrics.json baseline-2/ci-test-metrics.json \
  sharded-1/ci-test-metrics.json sharded-2/ci-test-metrics.json
```

`ci-test-comparison.json` reports median elapsed time, the observed range, setup and
fixture phase costs, runner-minute change, skipped cases and run links. It rejects
failed or incomplete evidence, duplicate inputs, different revisions and differences
in parametrized test coverage. Retain failed attempts as well when assessing flakes.
If the hosted API had not finalized all job timestamps, refresh that run's metrics
using `--repository OWNER/REPO --run-id ID --attempt N --shards 1` (or `2`) and
`--timings DIR` pointing at its verified `source-test-timings-<attempt>` artifact.
API refreshes require `GITHUB_TOKEN` with repository Actions read access.

The clean baseline at `6285d06` was 774.24s for 4,914 unit passes and 1,387.71s
for 501 browser passes; a duplicate unit run took 1,250.21s on the same revision.
Hosted comparison runs of the new partition are **pending**; local checks and ideal
division of those baseline times do not establish a measured speedup.

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
The build stamps AppStream's newest release from the wheel's own metadata before
exporting it. The packaged smoke check verifies that exported version too, so signed
repository version checks track the actual candidate rather than the checked-in release history.

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
