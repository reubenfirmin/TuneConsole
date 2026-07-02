# Packaging

Build TuneConsole (package `yt-playlist`) as a **Flatpak** (Linux) or a **self-contained `.app` +
`.dmg`** (macOS). Both launchers start the local server and open it in your browser automatically
(`yt-playlist --open`; the Flatpak opens the host browser via the OpenURI portal).

> **You also need the TuneConsole browser extension.** The app doesn't talk to YouTube Music
> directly — a lightweight Chrome/Chromium extension bridges your signed-in YouTube Music tab to the
> local app over `127.0.0.1:8765`, so your session never leaves the browser. Install it from the
> Chrome Web Store (or load `../extension/` unpacked) and keep a `music.youtube.com` tab open.
> Without it the app still runs, it just has nothing to sync.

Where the app keeps its data:

- **Linux/Flatpak:** `~/.var/app/com.tuneconsole.TuneConsole/{config,data}` — its private sandbox dir.
- **macOS:** `~/.config/yt-playlist` and `~/.local/share/yt-playlist` (override with `YT_PLAYLIST_HOME`).

`paths.py` honours `$YT_PLAYLIST_HOME`, then `$XDG_CONFIG_HOME` / `$XDG_DATA_HOME`, then the usual
`~/.config` / `~/.local/share`.

---

## Linux — Flatpak

The sandbox is deliberately tight: the only host permission is `--share=network`. No host filesystem
access is granted; config, `state.db`, and backups live in the per-app directory (Flatpak redirects
`XDG_*_HOME` there). Opening the browser uses the OpenURI portal. Downloaded share `.txt` files are
written by your browser, which runs outside the sandbox.

### Prerequisites
```sh
flatpak install flathub org.freedesktop.Platform//24.08 org.freedesktop.Sdk//24.08
# flatpak-builder: e.g. `sudo dnf install flatpak-builder` or `flatpak install flathub org.flatpak.Builder`
```

### Build
```sh
# from the repo root
uv build --wheel                                   # -> dist/yt_playlist-0.1.1-py3-none-any.whl

cd packaging/flatpak
./generate-pip-sources.sh                          # -> python3-requirements.json (needs network, once)

flatpak-builder --user --install --force-clean build-dir com.tuneconsole.TuneConsole.yaml
```

### Run
```sh
flatpak run com.tuneconsole.TuneConsole            # starts the server + opens the browser
flatpak run com.tuneconsole.TuneConsole --port 9000   # extra args pass through
```

Regenerate `python3-requirements.json` whenever `[project.dependencies]` changes, and rebuild the
wheel (`uv build --wheel`) whenever the app code changes, before re-running `flatpak-builder`.

### Notes
- The repo ships an MIT `LICENSE` at its root, matching `project_license` in the metainfo.
- **Publishing on Flathub:** a submission-ready manifest + vendored deps + step-by-step guide live in
  [`../flathub/`](../flathub/README.md). It fetches the wheel from a tagged GitHub Release (the
  `release.yml` workflow attaches it) and the metainfo screenshots from the deployed site.

---

## macOS — PyInstaller `.app` + `.dmg`

```sh
cd packaging/macos
./build.sh
```

Produces `dist/TuneConsole.app` and `dist/TuneConsole-0.1.1.dmg`. Python and all dependencies are
embedded — nothing is required on the target Mac.

### First launch (unsigned)
The bundle is unsigned, so Gatekeeper blocks a double-click the first time. Right-click the app →
**Open** (or System Settings → Privacy & Security → **Open Anyway**). After that it opens normally.

The server has no window of its own — it runs in the background and opens your browser. To stop it,
right-click the Dock icon → **Quit** (or quit from Activity Monitor).

### Custom icon (optional)
`build.sh` calls `make-icns.sh`, which renders the SVG to `TuneConsole.icns` if `rsvg-convert`
(`brew install librsvg`) and `iconutil` are present. Without them the app gets PyInstaller's default
icon — harmless.

### Signing & notarization (optional, for distribution)
With an Apple Developer ID:
```sh
codesign --deep --force --options runtime --sign "Developer ID Application: NAME (TEAMID)" "dist/TuneConsole.app"
xcrun notarytool submit "dist/TuneConsole-0.1.1.dmg" --apple-id you@example.com --team-id TEAMID --wait
xcrun stapler staple "dist/TuneConsole.app"
```
