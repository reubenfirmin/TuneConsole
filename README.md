# TuneConsole

A local music library manager and recommender for YouTube Music. Browse your
library, make mixes, listen to radio, explore related songs and import Spotify
playlists or saved albums. Recommendations run on your computer; your YouTube
Music sign-in stays in your browser.

[Install TuneConsole](https://tuneconsole.com/#install) ·
[Screenshots](https://tuneconsole.com) · [Privacy](https://tuneconsole.com/privacy)

## Run from source

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.

```sh
uv sync
uv run yt-playlist
```

Open <http://127.0.0.1:8765>. In Chrome, Chromium or Edge, open
`chrome://extensions`, enable **Developer mode**, choose **Load unpacked** and
select this checkout's `extension/` directory. Keep a signed-in
<https://music.youtube.com> tab open. The extension connects automatically.

For audio analysis in a source checkout, prepare the
[model assets](scripts/audio_model/README.md) and reload the extension.
Packaged extension builds include those assets. Playback audio and inference
stay in the music tab; only measurements and embeddings reach the backend.
Collection runs independently of open app pages and developer ratings.

For development reloads, use `uv run yt-playlist --reload`.

## Run in a container

Run on the same computer as your browser:

```sh
docker run -d --name tuneconsole \
  -p 127.0.0.1:8765:8765 \
  -v tuneconsole-data:/data \
  ghcr.io/reubenfirmin/tuneconsole:latest
```

Podman uses the same arguments. The volume retains your library, configuration,
logs and backups across container replacements. Images support Linux x86-64
and ARM64. See [packaging](packaging/README.md) for updates and local builds.

## Development

FastAPI serves Jinja templates with HTMX and Alpine.js. SQLite and managed local
files hold application data. The browser extension executes authenticated
YouTube Music requests. Frontend libraries are vendored.

Run a focused check:

```sh
uv run pytest tests/test_radio_recovery.py
```

| Reference | Contents |
| --- | --- |
| [Tests](tests/README.md) | Commands, fixtures, diagnostics and benchmark ownership |
| [Database maintenance](src/yt_playlist/core/README.md) | Migrations, resumable preparation, publication and recovery |
| [Recommendations](src/yt_playlist/rec/README.md) | Model, serving and publication boundaries |
| [Integrations](src/yt_playlist/integrations/README.md) | Provider access and retry contracts |
| [Frontend](src/yt_playlist/web/static/README.md) | JavaScript ownership and fragment lifecycle |
| [Extension](extension/README.md) | Pairing, capture, messages and diagnostics |
| [Packaging](packaging/README.md) | Builds, releases and updates |
| [Experiments](experiments/README.md) | Experiment results and model decisions |

HTTP access logging defaults off; set `YT_PLAYLIST_ACCESS_LOG=1` to enable it.
Set `YT_PLAYLIST_LISTENING_CHARTS=0` to disable the Charts journal and Listening
analytics and their background work. Developer vibe ratings default off;
`YT_PLAYLIST_AUDIO_VIBES=1` enables them without changing normal collection.
Use the [audio model settings](src/yt_playlist/rec/README.md#audio-model-settings).
