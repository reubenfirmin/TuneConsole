# Browser extension

The extension connects the local TuneConsole app to a signed-in YouTube Music tab.
The worker maintains an authenticated local WebSocket; allowlisted API requests run
inside the music tab using its session. Cookies are never pasted into the app.

## Setup

1. Run `uv run yt-playlist` (default `127.0.0.1:8765`).
2. Prepare [audio assets](../scripts/audio_model/README.md) for a source checkout.
3. Open `chrome://extensions`, enable Developer mode and load this directory unpacked.
4. Open `https://music.youtube.com`, sign in and keep the tab open.

The toolbar shows connection/setup controls. Fresh installs open the setup website
unless the app is already connected; updates and browser restarts do not open setup
tabs. The music-tab reminder appears after a sustained disconnect and dismissal lasts
for that tab's session. The public site's Local Player button reuses the local app tab.
Without the running app, the extension does not provide recommendations or buffer
offline listening history.

Change `bridgeUrl` and `appOrigin` in `worker/config.js` together when using another
local port. Reload the extension after source changes. Updates repair sensors in open
music tabs without reloading playing or partially played media.

## Permissions and privacy

- Site access is limited to YouTube Music, TuneConsole and local app hosts. There is
  no broad `tabs` or `history` permission.
- `cookies` supplies signed-in auth; `alarms` checks reconnection; `scripting` installs
  controls; `storage` retains pending audio metadata, never raw audio.
- The backend accepts the pinned extension origins. Only allowlisted `youtubei/v1/`
  requests can cross the fetch bridge; worker and receiver both validate them.
- Playback and curation observations go to the local app. The public website receives
  connection/open status only, with no track, account or library data.
- Raw audio, decoded PCM and spectra stay in the music tab and are not persisted.
  Only bounded measurements and the versioned 200-value embedding reach the backend.

## Playback and curation

Playback attribution uses the actual player's video/playlist IDs, never the address
bar. Metadata and account verification are separate from presence. Stale/mismatched
credits cannot create a recording or update its metadata/rating. Unknown accounts
cannot record listening. Verified known uploads can supply exact play observations
under the compatible protocol's narrower metadata rules.

One audible, unmuted music tab owns controls and observations. Playback launches and
library fetches share tab creation/readiness coordination. Secondary muted/paused tabs
cannot replace the audible owner's card.

Two-second presence heartbeats carry the player's per-song clock. The app interpolates
between samples and reconciles seeks/pauses. Full duration comes from exact-video
metadata; buffered duration and a media timeline spanning several songs are unsuitable.
Unknown/non-finite times remain unknown. Raw behavior events include track exit, end,
play/pause, roughly 30-second recovery ticks, volume and tab departure.

The fetch observer reports ratings, playlist edits, library/album feedback, follows
and share intent without altering the page's original promise. Ratings include settled
success/status; other curation observations report intent rather than confirmed success.
The app's isolated-world API calls are excluded from observation.

Uncertain delivered edits are never replayed. Only a proven missing receiver permits
one safe retry. Desired-rating recovery stays on its original tab for at most 30 seconds,
rechecking song, account, expiry and cancellation before any click. A lost reply or
unconfirmed click ends recovery. Dislike-and-skip confirms the thumb before advancing
and avoids a second skip after uncertain delivery. API rating success remains independent
of native control reconciliation.

## Audio collection and preview

Collection is owned by `worker/audio_sampler.js` and `worker/audio_collection.js`.
It continues without an app document and posts results directly to
`/audio-vibes/samples`. Only recognized extension origins can use this cross-origin
collection endpoint; other mutations retain same-origin protection.

`audio_capture.js`, `worker/audio_capture.js` and `audio_capture_frame.html`/`.js`
share capture and tab-local inference. The separate `/audio-preview` tool uses
`AudioCaptureSession` from `audio-capture-session.js`. The collection port is
`tc-audio-collection`, protocol 1; stable preview-named wire identifiers remain compatible.
Only a top-level page on the configured app origin/port can open an observing app port.

| Behavior | Contract |
| --- | --- |
| Scheduled excerpts | Ten seconds at 30 seconds, halfway and two-thirds through playback. Only the first point is available until matching full-duration metadata arrives. |
| Clock | Wait in the music tab against the native per-song clock; missing/stale clocks do not authorize capture. No seeking or padded audio. |
| Interruptions | Pause, seek, source/track changes and deadlines cancel unfinished capture. Completed recordings may finish inference after a track change. |
| Retry | Replace missed points as playback permits; retry failures after 5, 10, 20 and at most 30 seconds, without duplicating saved points. |
| Preview | One capture owner across collection and preview. Preview cancels unfinished collection work; completed excerpts finish durable delivery first. Collection resumes after the last preview disconnects. |
| Durable delivery | Store up to 12 immutable metadata payloads before posting; back off 5–60 seconds. Only a matching sample UUID receipt removes one. Restarts/lost replies resend idempotently. Full or rejected queues retain data and pause capture. |
| Upgrade | Import older app outboxes before removing them, only after extension storage acknowledges. |

Inference uses a private MessageChannel and packaged frame/worker. The broker accepts
typed bounded metadata and frozen MusiCNN embeddings, rejecting PCM, spectra and
arbitrary arrays. Embeddings are mean full-patch pre-activation features, identified
by artifact, layer, graph policy and pooling. Every production ZIP includes verified
runtime/model assets; incomplete or mismatched assets prevent packaging/capture.

The preview shows four mood classifiers, DEAM arousal/valence and optional general
tags. Those diagnostic scores are uncalibrated. Production vibe recipes and adoption
evidence belong in the [model logs](../experiments/audio_lab/README.md#model-provenance) and
[recommendation reference](../src/yt_playlist/rec/README.md).

### Developer feedback and storage

`YT_PLAYLIST_AUDIO_VIBES=1` enables the developer prediction/feedback banner, including
from ignored `.env.local`. It does not enable collection or sample APIs, which already
work without it. With the flag off, rating modules, click queues and feedback writes
remain disabled.

Each playback occurrence retains its card and explicit multi-label edits. Load saved
answers before accepting clicks; a replay marks the last exact-video answer Previous
answer. Opening, sampling or dismissing a card creates no label. Preserve unanswered
cards and pending edits across navigation. Labels freeze the sample IDs and prediction
shown at the click; later inference never rewrites them. Predictions require three
saved excerpts; a best guess below the provisional cutoff records `best_available`
and its top-two gap.

| Product choice | Feedback meaning |
| --- | --- |
| Relax | Calm, soothing, unhurried; settling down and releasing tension |
| Focus | Steady, absorbing, unobtrusive; concentration without repeated distraction |
| Cruise | Easygoing, moderately lively company for everyday listening |
| Energize | Driving, animated, propulsive; momentum and activity |
| Lift my mood | Positive, buoyant, encouraging; emotional lift at any energy |
| Let off steam | Forceful, hard-hitting, cathartic; intensity even when slow |

Immutable sample JSON lives under `sample-attributes` beside the app database.
`sample_attributes` indexes files; `measurements`/`measurement_samples` hold scores
and links; `audio_vibe_runs`/`audio_session_labels` retain playback and frozen judgments.
The current taxonomy is `audio-session-targets-1`. Older feedback taxonomies remain
unchanged. Join playback comparisons on `run_id`; unknown uploads remain provisional.

Samples retain 27 scalar attributes, six mood outputs, 50 tags and compatible frozen
embeddings when available. Essentia measures tempo/confidence/beat variation and excerpt
EBU R128 loudness at 44.1 kHz; Meyda descriptors use 2048-sample Hann windows with a
1024 hop, before separate 16 kHz model input. LUFS describes excerpts, Bark loudness
has arbitrary units, and peak-normalized RMS describes gain-independent structure.
The inspector exposes coverage, positions and replacement provenance. Measurements
and developer labels are distinct from recommendation-learning evidence.

The access-only probe lives in
[`experiments/audio_lab/audio_probe.js`](../experiments/audio_lab/audio_probe.js).
Use the [lab staging instructions](../experiments/audio_lab/README.md)
for a disposable extension; it is excluded from production. Audio fixtures use
Chromium's `--disable-audio-output` to process PCM without speaker hardware.

## Radio inspection

On air uses a saved YouTube playlist. The on-demand `radio_session.js` companion
disables native Autoplay and asks Music to refetch confirmed playlist changes without
seeking or replacing the player. Start with three songs and keep one saved ahead;
directions replace only the unplayed suffix. Reopening reattaches to the session.

Confirm adoption against native upcoming tracks, independently of the browse page.
An unconfirmed change gets at most three refresh attempts eight seconds apart. After
adoption, refresh visible rows once; routine polls do not repeatedly navigate. Explicit
song Play dispatches after its saved occurrence is confirmed, before removing old
upcoming songs. Checkpoints preserve cleanup history without replaying playback commands.

In the YouTube Music page's DevTools console:

```js
copy(JSON.stringify(window.__tcRadioSession.inspect(), null, 2))
```

Use `inspect({methods: true})` or `source(owner, method)` for native method inspection.
Reports distinguish `playback_synced` and `page_synced`. Bounded traces omit cookies
and request headers; routine activity stays quiet.

## Code and protocol owners

| Files | Responsibility |
| --- | --- |
| `background.js`, `worker/config.js` | Bootstrap/listeners and shared configuration |
| `worker/transport.js`, `worker/requests.js` | WebSocket ownership, allowlist, auth and safe delivery |
| `worker/music_tabs.js`, `worker/playback.js` | Shared tab creation, playback ownership, controls and rescue |
| `worker/ratings.js`, `worker/sensors.js` | Bounded desired-rating recovery and isolated/MAIN repair |
| `worker/setup.js`, `music_setup.js`, `setup.*` | Connection/setup panel and music-tab reminder |
| `worker/site.js`, `site_player.*` | Connection-only public-site status and app activation |
| `worker/radio.js`, `radio_session.js` | Saved-playlist companion and native adoption |
| `content.js`, sensor modules | In-page fetch, MAIN observations and isolated forwarding |

| Boundary | Contract |
| --- | --- |
| MAIN to isolated | Same-window `__tcNow`/`__tcPlayer`, sensor protocol 4 and compatible revisions; separate `videoVerified`/`metadataVerified`. `__tcCuration` carries bounded observed requests. |
| Isolated to MAIN | `__tcSensorProbe: {version, id}`; fresh sample echoes `__tcSensorProbeId`. Health/control checks use this handshake. |
| Worker to isolated | `sensor-ping` reports receiver/MAIN revisions and bounded diagnostics; `main: false` checks fetch readiness. `fetch` returns status/body/timezone. |
| Isolated to worker | `play`, `now-heartbeat` and `pevent`; resolve tab ownership before forwarding. |
| Local bridge | `/bridge/ws`, authenticated extension origin. Requests `{id,url,method,body}` return `{id,status,body,timeZone?}`. `{ping:1}` is keepalive. |
| Controls | `navigate`, acknowledged `rate`/`next`/`radio-session` (legacy `playlist-session-poc`), `playpause`, `pause`, `ensure-playing`, deferred `refresh-view` and `resync-now`. |
| Native reconciliation | `ratingId`/absolute `expiresAt`, `cancel-rating`, then one pending and one terminal `rating-reconciliation` frame. Revalidate player/account immediately before a click. |
| Radio companion | `__tcRadioSession.update({playlist,videoIds,refresh})` reports native/page adoption and borrows the existing playback tab owner. |
| Public site | Exact-origin `local-player-status` and `open-local-player` only. |

Protocol versions describe compatibility; revisions identify installed implementations.
Compatible newer sensors remain installed; stale receivers refresh in place. A legacy
anonymous-listener guard cannot safely be replaced, so unsupported controls require a
page reload while playback remains connected. `/bridge/status` exposes up to eight
tab revision/capture snapshots without song/account values. A responding receiver alone
does not prove MAIN health or current code.

Confirmed Now Playing controls retain displayed state while pending and verify the
displayed video/action. Play requires observed readiness/clock progress; manual Pause
cancels delayed startup rescue. Missing bindings, blocked playback and uncertain delivery
stay within the action's controls, following the [UI error policy](../AGENTS.md).

## Verification and packaging

Run focused checks directly; only global suites acquire the shared suite lock:

```sh
uv run pytest tests/test_extension_*.py tests/test_playback_rescue.py
uv run pytest -m browser tests/browser/test_extension_lifecycle_browser.py tests/browser/test_extension_site_player_browser.py
scripts/build-extension.sh
```

Node checks execute production modules with explicit doubles. Chromium fixtures exercise
real extension worlds, media, tabs, WebSockets and persistence against a local TLS Music
fixture. They do not establish current signed-in Music markup, real autoplay policy or
OS focus behavior. See [test setup](../tests/README.md).

Live Radio acceptance uses `uv run pytest --run-live-radio
--live-radio-trace=/tmp/tuneconsole-radio-live.json -s tests/test_radio_live.py`.
It resumes/changes real playback and waits for natural progression; ordinary runs skip it.
Before store submission, check signed-in playback, account/metadata attribution,
pause/rating controls, concurrent launch/fetch, updates while playing, deferred playlist
refresh, Radio adoption and the public site's disconnected/connected setup flow.

For a store update, increase `manifest.json`'s version and upload
`dist/tuneconsole-extension-<version>.zip`. Packaging verifies models/libraries/runtime
and licenses, and excludes the development key, README, hidden files, source maps and
research probes. See [asset preparation](../scripts/audio_model/README.md).
