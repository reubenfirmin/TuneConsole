# Flathub submission

This directory is the Flathub-ready submission for **TuneConsole** (`com.tuneconsole.TuneConsole`).
Flathub hosts the app manifest in its own repo and builds it on their infrastructure, so this is
separate from `../flatpak/` (which is the local dev build).

## What's here

- `com.tuneconsole.TuneConsole.yaml` — the submission manifest (fetches the wheel from a GitHub
  Release + the branded files from the pinned git tag; deps from the file below).
- `python3-requirements.json` — vendored, checksummed Python deps (copy of the one produced by
  `../flatpak/generate-pip-sources.sh`).

## Prerequisites (do these first)

1. **App-id owns the domain** — `com.tuneconsole.TuneConsole` ↔ `tuneconsole.com`. ✅
2. **Deploy the site** — the metainfo screenshot URLs are `https://tuneconsole.com/shots/*.png`;
   Flathub validates that they resolve, so the site must be live.
3. **Cut a `v0.1.1` release** with two assets Flathub can fetch:
   - The wheel `yt_playlist-0.1.1-py3-none-any.whl` (the `release.yml` workflow attaches it on tag).
   - (The macOS `.dmg` is separate and not needed by Flathub.)

## Fill the placeholders

In the manifest:

- `commit:` → `git rev-parse v0.1.1`
- the wheel `sha256:` is filled for the wheel built from this tree; if you rebuild the wheel for the
  release, re-hash it: `sha256sum dist/yt_playlist-0.1.1-py3-none-any.whl`.

## Submit

1. Fork `github.com/flathub/flathub`, branch `com.tuneconsole.TuneConsole`.
2. Add `com.tuneconsole.TuneConsole.yaml` and `python3-requirements.json` from this directory.
3. Open a PR. Flathub's bot builds it; a reviewer checks the manifest, metainfo, and that the app
   runs. Iterate on their feedback.

## Notes

- Reviewers sometimes prefer building from source over a prebuilt wheel. If asked, switch the
  `tuneconsole` module to build the wheel in-sandbox: add a `python3-hatchling.json` (generate with
  `flatpak-pip-generator hatchling`) as a module, then replace the `pip3 install … .whl` command with
  `pip3 install --no-index --no-build-isolation --prefix=${FLATPAK_DEST} .` (the git checkout is the
  build context). The prebuilt-wheel path here is simpler and matches the validated local build.
- Regenerate `python3-requirements.json` (and re-copy it here) whenever `[project.dependencies]`
  changes.
