# TuneConsole session bridge

Browser extension data plane for TuneConsole
(`docs/superpowers/specs/2026-07-01-extension-data-plane-design.md`).

**What it does:** the service worker holds a persistent, allowlisted WebSocket connection to the
local TuneConsole backend. When the backend needs signed-in YouTube Music data, it sends a
request frame over that socket; the extension checks the request against an allowlist
(`youtubei` endpoints only), computes the `SAPISIDHASH` auth header from your live session
cookie, and asks the content script running on the `music.youtube.com` page to make the actual
fetch. Because the fetch runs in the page, it is visible in the normal DevTools Network tab, no
extension-specific inspector needed. No cookie paste, no OAuth, no Google Cloud project.

## Connect the extension to the backend

Pairing is seamless: there is nothing to paste. The backend recognizes the extension by its
pinned identity (the `key` in `manifest.json` fixes the extension id, and the backend accepts the
WebSocket only from that `chrome-extension://` origin, which a web page cannot forge).

1. **Start the app:** `uv run yt-playlist` (the bridge listens on `127.0.0.1:8765`).
2. **Load the extension** (Chrome/Chromium/Edge):
   - Go to `chrome://extensions`
   - Turn on **Developer mode** (top right)
   - Click **Load unpacked** and select this `extension/` directory
3. **Open** `https://music.youtube.com` while **signed in**. Keep that tab open; it is where the
   authenticated fetches run. The extension connects to the backend automatically.

## What you should see

- The extension toolbar icon opens a connection and setup panel. It explains that mixes, library
  tools and listening history require the local TuneConsole app. A fresh install opens
  `https://tuneconsole.com/extension-setup`, a dedicated page with app downloads, launch steps, and
  Mac first-launch approval instructions. If the app is already connected, it opens the app instead.
  Starting the app from setup replaces the install instructions with step 3: “Time to rock out!”
  and an Open TuneConsole button. Reloading an unpacked
  development extension also runs this check, as does
  re-enabling the extension on Chrome 155 and newer. The toolbar and YouTube Music reminder link
  to this same page. No extra management permission is required.
  Setup reuses an existing TuneConsole website tab, and opening the app reuses its existing tab.
- On YouTube Music, an app that remains disconnected for five seconds gets a setup reminder.
  Starting the app removes it automatically. Dismissal lasts for that tab's session, including page
  reloads; the toolbar panel stays available. Brief disconnects do not flash the reminder. Store
  updates and browser restarts never open a setup tab. Setup does not interrupt playback.
- The extension alone keeps YouTube Music working normally; it does not provide standalone
  recommendations or save listening history while the app is stopped. Playback events are relayed
  to the running local app rather than buffered for offline history.
- The app shows the extension connection status. Routine connection and playback activity
  stays quiet in the browser console.
- When the app makes a request (for example, a sync), the request appears in the YTM tab's
  DevTools **Network** tab, filtered for `youtubei`, right alongside YouTube Music's own API
  calls. That visibility is the whole point: nothing is hidden in the service worker.
- Requests outside the allowlist (anything that is not a `youtubei/v1/` call) are refused with a
  "blocked by allowlist" error instead of being sent.
- On `tuneconsole.com`, a **Local Player** badge appears in the header. Neon green switches to an
  existing local app tab (including `localhost` tabs and tabs in other windows) or opens a new one;
  gray opens platform-specific instructions for installing or starting it.
  The badge follows the local bridge connection automatically. Only
  the connection status is exposed in the page; no library or playback data is shared with the site.

## Experimental direct audio access check (#406)

The normal app's **Tools > Audio analysis preview** now connects to this bridge
automatically. With the optional local research model installed, leave YouTube
Music playing and open `/audio-preview`: it captures one ten-second excerpt per
playing song and displays mood predictions without a capture click or
sharing picker. **Stop analysis** cancels the job; **Analyse again** is an optional
retry. Reload the unpacked extension and restart the app after updating these files.

The app page opens a Chrome runtime port. The worker accepts only a top-level
app page on the configured local app origin (including its localhost
alias and exact port), and selects the existing playback-owner tab. Capture,
offline decoding and inference all stay in that YouTube Music tab. A private
MessageChannel transfers 16 kHz mono PCM into an extension-owned frame and worker
embedded there. Bounded scores, diagnostics and one versioned 200-value frozen
MusiCNN embedding per excerpt cross runtime ports to the app. The embedding is
the mean of full-patch pre-activation features; model artifacts, layer, graph
policy and pooling identify its source. The broker permits only this typed
embedding payload and rejects PCM, spectrograms and arbitrary feature arrays.
Analysis needs neither a sharing picker nor a new user gesture.
Only one preview job runs across pages, including inference. Stop, closed pages,
disconnected ports, changed playback and deadlines release capture and pending
work. The source recording remains unverified and results do not update metadata
or recommendations. A brief recorder flush margin is discarded; model input
contains exactly ten seconds of captured audio, without zero padding.

The preview shows four dedicated mood classifiers (relaxed, aggressive, happy,
sad) and DEAM arousal/valence regression using the same MusiCNN embeddings.
Scores remain uncalibrated; they do not establish useful vibe selection yet.
General music tags remain available under a disclosure. Nothing is adopted into
On Air or Mix Tapes recommendations.

Normal app pages collect automatic ten-second samples at 30 seconds, halfway and
two-thirds through ordinary playback when the installed extension includes the
optional analysis model. The separate `tc-audio-collection` connection checks the
local model manifest before permitting capture. Public archives exclude these
research assets, so automatic collection remains idle there. An unavailable model
causes a quiet one-minute retry rather than repeated recordings and failures.

The opt-in `YT_PLAYLIST_AUDIO_VIBES=1` app flag adds only the developer
prediction/feedback banner. Collection and sample APIs work with this flag off;
rating modules, saved click queues and feedback writes stay disabled. The flag
also reads the checkout's ignored `.env.local`. A feedback card appears as soon as a new song starts, and
all six choices can be saved before any analysis succeeds. Saved choices are
restored when the panel opens. On a replay, the latest answer for that exact
video is preselected and marked **Previous answer**. Choices wait for saved
answers to load before accepting clicks. Sampling and reopening the panel do
not save or clear an annotation; an explicit edit saves the displayed choices
with the current playback's sample IDs, leaving the earlier answer intact.
Unanswered cards stay
in the shared browser stack across songs and navigation. Each saved excerpt
lights one green indicator; failed or pending checks stay unlit.
One app tab owns capture. The music tab waits for each sampling point against
its media clock, so a backgrounded app's timers cannot delay the next excerpt.
Other open app tabs display the same results. Typed, bounded sample attributes
are saved in immutable JSON files referenced by `sample_attributes`;
`audio_vibe_runs` and `audio_session_labels` retain playback and feedback state.
Labels preserve the exact
sample set and experimental prediction shown when selected. Samples retain all
50 tags, six mood outputs and, when available, the 200-dimensional frozen embedding.
Compatible embeddings use the packaged Emotify emotion expert and six-vibe
calibrator; older or incompatible captures retain the uncalibrated mood policy.
The checkpoint's exploratory acceptable-choice result is 38/55 (69.1%), not a
population accuracy estimate. Background preparation gives measured recordings
audio-based fits and propagates bounded artist/genre evidence to unmeasured
recordings. Each measured recording supplies at most one vote; propagated
predictions never become new measurement votes. Developer annotations are kept
separate from this preparation and are never rewritten by it.
No prediction appears until three excerpts have been saved; incoming analysis
does not change an earlier answer or the evidence attached to it. Six cross-genre listening
purposes are shared with Mix Tapes. Product names and feedback descriptions are separate:

| Mix Tapes session | Attributes shown in the feedback popup |
| --- | --- |
| Relax | Calm, soothing, unhurried. Helps me settle down and release tension. |
| Focus | Steady, absorbing, unobtrusive. Supports concentration without repeatedly grabbing my attention. |
| Cruise | Easygoing, moderately lively, comfortable company. Keeps everyday listening enjoyable without pushing me toward a particular mood. |
| Energize | Driving, animated, propulsive. Gives me momentum and makes me feel more active. |
| Lift my mood | Positive, buoyant, encouraging. Gives an emotional lift, whether gentle or energetic. |
| Let off steam | Forceful, hard-hitting, cathartic. Delivers intensity and impact; can be slow and heavy. |

The popup exposes both the attribute phrase and full description for every choice.
Its predictions and fit breakdown use the attribute phrases. The banner permits multiple
labels and saves each explicit click automatically. The product modal also retains its neutral option.
Dismissal creates no training label. Labels can be saved before
the first excerpt, with no prediction attached. Missed or interrupted points are
replaced as soon as playback allows, without seeking or duplicating saved points.
Failed captures retry after 5, 10, 20, then at most 30 seconds, while the song has
enough audio left. A delayed HTTP status or corrected player clock cannot cancel
a recording: the YouTube tab checks the actual media element, player song, pause,
seek, source and track lifetime. Completed recordings may finish analysis after
the song changes. Unfinished checks remain unlit while capture or retry is pending.
Future checks wait inside the YouTube tab using MAIN's per-song player clock,
anchored to native media progress. The media element's raw timeline can span
several songs and is retained separately for diagnostics. Missing or stale song
clocks do not authorize a scheduled capture. Reloading an app page preserves
completed points. Reload the extension and app page after capture-code changes;
backend/schema changes additionally require an app restart.

The **Audio details** dialog shows all 27 measured scalar
attributes, six mood outputs and 50 tags for each excerpt. The optional
`essentia-csp-audio-2` runtime measures tempo (including native confidence and beat
interval variation) and EBU R128 loudness on 44.1 kHz audio. Meyda 5.6.3 adds
spectral and perceptual descriptors using 2048-sample Hann windows with a
1024-sample hop. Measurements precede the model's separate 16 kHz mono input.
LUFS and loudness range describe only the captured excerpt; Bark loudness is an
arbitrary-unit descriptor, and peak-normalized RMS is gain-independent amplitude
structure. None of these new attributes is yet used to infer a vibe.

Scalar attributes, model/artifact versions, scheduled and actual positions,
replacement provenance and observed title/artist are durable in JSON files under
`sample-attributes` beside the local app database
(`~/.local/share/yt-playlist/state.db` by default). `sample_attributes` holds each
file's recording, timestamp and collection identity. Measured scores live in
`measurements`, with sample links in `measurement_samples`. `audio_session_labels`
holds the selected vibes, taxonomy version, frozen sample IDs and prediction
visible at labeling. The current taxonomy is `audio-session-targets-1`, keyed with
the playback run so future taxonomies can coexist. Earlier judgments remain unchanged
in `audio_vibe_feedback` (original presets) and `audio_vibe_labels` (four sound targets),
and are never reinterpreted as labels for these listening purposes.
Join on `run_id` for playback comparisons. Unknown uploads retain separate
provisional recording identities until verified. New captures also retain frozen
embeddings in their immutable sample JSON; older files remain usable without
them. No audio or spectra are persisted. These observations
do not alter the selected Mix Tapes vibe or recommendation training.

Every completed three-excerpt set produces a best guess, including when all fits
fall below the provisional cutoff. Such guesses retain `selection_method=best_available`
and the gap between the top two fits; they are hypotheses to correct. The measurement
inspector reports attribute coverage, tempo/confidence and excerpt-loudness ranges,
and replacement excerpts. Missing attributes and varied tempo estimates remain visible.

The initial fits are engineering hypotheses. Energize follows activation regardless
of positive/dark tone; Lift my mood uses positive/sad mood outputs independently of
energy; Let off steam emphasizes force even at low energy. Cruise favors a measured
middle in energy and low force. Focus has only coarse calm/low-force proxies in the
preview: vocal presence, steadiness and distraction are not established by the current
heads. Production catalog profiles can additionally supply instrumentalness and
density. Listening judgments are needed to validate these distinctions; measurements
do not prove that a track supports concentration or delivers catharsis.

Prepare the optional local research files after fetching models/building the
runtime as described in `experiments/audio_lab/README.md`:

```sh
python3 experiments/audio_lab_tools/prepare_extension.py
```

Then reload the normal unpacked extension and the normal app page. Re-run
preparation after changing worker source or artifacts. `extension/audio_model/`
is ignored and explicitly excluded from Web Store ZIPs; this POC does not authorize
distribution of the research models. The normal bridge otherwise keeps its
permissions unchanged; its CSP allows packaged WebAssembly without JavaScript eval.

### Access-only diagnostic

The earlier `audio_probe.js` access-only diagnostic is retained for explicit
developer injection and browser tests. Normal startup and sensor repair no longer
install it, so no separate three-second capture runs outside the app's opt-in
feature. When explicitly injected into the extension's isolated YouTube Music
world, its **Audio access check** banner attempts one three-second check as soon
as music is playing. No capture button or sharing picker is required.
**Run check again** is an optional retry; **Stop check** cancels the current check.
There is no microphone, downloaded recording or generated sound.

The check uses `HTMLMediaElement.captureStream()` and MediaRecorder's lossless
32-bit float PCM WebM output, with an 8 MiB memory limit. Offline decoding reads
the PCM without creating a live AudioContext or audio output, so analysis does
not need a gesture to unlock Web Audio. Browser tests with default autoplay
rules verify both capture and decoding with zero transient or prior page
activation; no debugger-supplied gesture or autoplay override is used.

The result distinguishes attached DRM media keys, cross-origin rejection, a
usable nonzero signal, no signal, and playback/capture interruptions. Expand
**Audio access report** or choose **Copy report** to share the diagnostic JSON.
It includes signal measurements and whether playback continued, with no signed
stream URL, cookie, audio samples, or verified recording association. Silence is
inconclusive; it is not reported as DRM. Success applies only to the tested player
state, not every YouTube Music song or account. User-reported live checks on
2026-10-25 confirmed both the initial click-started path and automatic PCM capture
from a YTM blob source without attached DRM keys, with playback continuing.
The automatic check decoded 3.008 seconds of stereo audio at 48 kHz with RMS
0.22985 and no live AudioContext. Its transient and historical page activation
were both true, so this live report confirms automatic capture in that playback
state; zero-activation coverage comes from the browser tests above. Schema 2
reports the automatic/manual trigger and page activation at capture start.

The probe creates no audio output; the original player remains audible. The
three-second check, **Stop check**, dismissal, pause, seek, source change and
navigation release its own capture tracks and recorder without controlling the
player. Pending decode results after cancellation are discarded and their PCM
is zeroed. Dismissal lasts for the tab's session, including reloads. This is an
access probe, not the tag-model preview, and does not change On Air or Mix Tapes.
Automated audio tests use Chromium's `--disable-audio-output` FakeAudioManager
so they process PCM without opening speaker hardware.

## What the extension observes and sends

The extension acts as a sensor for your listening and curation behavior on YouTube Music, capturing
both playback events (play/pause, track changes, completion) and curation actions (likes, playlist
edits, follows). All events land in the local database; nothing leaves your machine.

### Playback events

Track and playlist IDs come from the playing video's player state, so browsing another song or
playlist does not change listening attribution. The DOM fallback uses player-bar links or artwork;
it never borrows IDs from the address bar. If the playing video cannot be identified, it sends
presence updates without recording a play.

Player-bar metadata is used when its track ID matches the playing video, preserving album artwork
and artist credits even when the artwork URL has no video ID. MediaSession metadata is used when
its artwork confirms that video. Ratings and album links are captured with the bar's track ID;
stale fields cannot be attached to a different song. Unverified player-only metadata updates
presence until matching metadata is available. Reconnect requests a fresh MAIN sample and preserves
its verification result. The player-bar fallback restores presence while MAIN is incomplete or
throttled; only explicitly verified MAIN metadata can record a play. Playback history also waits
for a verified account; fallback presence retains the last verified account.

Now Playing and playback controls select the same music tab, preferring audible, unmuted playback.
Muted or paused secondary tabs cannot replace an audible tab's card. Playback launches and library
requests share tab-creation coordination, and concurrent library requests share the startup wait
for a new or already-loading music tab before sending fetches.

The two-second Now Playing heartbeat carries the player's per-song elapsed time through
both extension worlds and the worker. The app estimates progress locally between heartbeats,
then reconciles seeks, track changes and pause state with the next snapshot. Timing comes from
the same player's getCurrentTime() API as its song ID. The server supplies the full length stored
for that exact video ID. Music's getDuration() can grow as media buffers, so it is never used
for the progress total, including when an older extension still sends it. An unknown catalog
length stays unknown until metadata arrives. The underlying video's media timeline can span
multiple songs, so neither MAIN nor DOM fallback uses it for progress.
The sensor rechecks the player's identity after reading timing to reject a changing sample.
Missing player timing remains unknown; progress never waits for the media stream to reset.
Reduced motion keeps the thin progress divider visible; unknown/non-finite times remain unknown.

The extension observes the video element and player UI to capture listening behavior. Each event
includes the video ID, playback position (in seconds), track duration, and current shuffle/repeat
mode (unknown values are empty strings):

- `track_exit`: you changed tracks (skip/replay); carries the exit position of the previous track
- `ended`: the track completed naturally
- `state`: you pressed play or pause
- `tick`: heartbeat every approximately 30 seconds while playing (session recovery if the tab dies)
- `volume`: you adjusted the volume (0 to 1 scale; muted = 0)
- `bye`: you closed or unloaded the tab

### Curation events

The extension observes requests to five curation endpoints and reports them at request time. The
server extracts kind-specific details from the request body (best-effort, since we observe requests
before we can confirm they succeed):

- `rate`: track ratings via `/youtubei/v1/like/{like,removelike,dislike}`
- `playlist_edit`: add/remove tracks via `/youtubei/v1/browse/edit_playlist`
- `feedback`: library/album saves via `/youtubei/v1/feedback`
- `subscription`: artist follow/unfollow via `/youtubei/v1/subscription/{subscribe,unsubscribe}`
- `share_intent`: share dialog opened via `/youtubei/v1/share/get_share_panel`

### Privacy

All events are stored locally in the app's database.
- Timestamps are assigned by the local server at the moment each event arrives.
- The fetch observer never alters or blocks requests; it only observes them. The app's own API
  calls run in the extension's isolated world, so they are structurally excluded from observation.

## Radio Session console

Home's Radio tab (`/#radio`) injects `radio_session.js` on demand.
It reads Music's native playback store, turns the identified Autoplay switch off, and asks Music's
native fetch handler to refresh the saved playlist after each confirmed change. The handler uses
the current playback endpoint and fetches the playlist from YouTube without navigating or seeking.
The session starts with three songs, then keeps one song saved
ahead; the four continuations on Home are proposals, and selecting one replaces only the
unplayed playlist suffix. Opening Radio reattaches to the current session without restarting it.
It confirms adoption against Music's actual upcoming tracks, separately from the embedded video
player and the browse page. Sending a refresh request alone does not confirm a direction switch.
An unconfirmed change gets at most three refresh attempts, at least eight seconds apart.
After playback adopts a change, the observer also clicks the playlist's sidebar link once to
refresh its visible rows. Browse refreshes have their own state and continue after playback is
confirmed; routine status polls do not keep clicking the link. The compact report includes
`page_synced` alongside `playback_synced`.
The observer does not play, pause, seek, dispatch store mutations, or supply playback items;
Music fetches and reconciles its own saved-playlist contents.

An explicit stream-song Play starts once the selected song's saved occurrence is confirmed.
The backend dispatches playback before removing the old upcoming songs, then reconciles that
suffix while Music loads the selected song. Checkpoints retain the protected history prefix
for cleanup after a restart, without retaining or replaying the playback command.
Until dispatch, a fresh player observation can be reused while the live heartbeat still
identifies the same song and playlist. Song changes, stale observations and cleanup require
a fresh browser sample. A click received during an observation or metadata pass takes
priority over the next maintenance phase.

After reloading the unpacked extension, start a session and open the **YouTube Music tab's**
DevTools console (page context). To copy the compact live playback report:

```js
copy(JSON.stringify(window.__tcRadioSession.inspect(), null, 2))
```

`window.__tcRadioSession.inspect({methods: true})` includes native method sources when needed.
`window.__tcRadioSession.source(owner, method)` returns one full native method, including
owners `ytmusic-app.navigator` and `ytmusic-app.playlistPollingHandler`.
Routine playback, connection, and Radio activity do not print to the browser console. Radio keeps
a bounded native-event trace for the inspection report. Unexpected service worker failures print
at most once per failure type per minute. Backend `Radio playback:` lines report saved, native,
and embedded-player upcoming IDs when those states change. The probe does not include cookies
or request headers.

## If it fails

Check the service worker console for the reason:
- No connection: confirm the app is running on `127.0.0.1:8765`. If you changed the port, update
  `bridgeUrl` and `appOrigin` in `worker/config.js` to match.
- "open music.youtube.com": no matching tab was found; open one and keep it open.
- "not signed in": the `SAPISID` (or `__Secure-3PAPISID`) cookie was not found; sign in to
  `music.youtube.com` and retry.
- "blocked by allowlist": the backend asked for a URL outside `youtubei/v1/`; this is expected
  behavior, not a bug.
- "response lost; the request may have completed": the tab stopped responding after delivery.
  Check YouTube Music before retrying an edit; the extension avoids replaying an uncertain request.

Extension updates inject fresh sensors into open music tabs without reloading them. Sensor recovery
checks both the isolated receiver and a fresh, versioned MAIN sample, and can repair MAIN even if
the isolated receiver still answers. It also defers reloads while media is playing or paused partway
through a track. Fetch readiness only requires the isolated receiver. Playlist edits refresh
matching views across music tabs when playback allows it. Manually opened local app tabs stay open;
the public site's Local Player button reuses an existing app tab.

## Files

- `background.js`: constructs modules and registers Chrome lifecycle listeners. Module factories
  register no listeners and do no work until called by this bootstrap.
- `worker/config.js`: shared constants and throttled warnings.
- `worker/transport.js`: sole WebSocket owner; reconnects after three seconds. Replies use the
  socket that received the request. Reconnect probes sensors before asking for a fresh snapshot.
- `worker/requests.js`: endpoint allowlist, cookie-derived auth, fetch delivery, and safe retries.
- `worker/music_tabs.js`: sole music-tab creation queue and shared fetch-readiness promise.
  Playback launches and library fetches use this same queue. Document/sensor waits run outside
  the queue so a launch can navigate an already-starting tab. Waits end on load, removal, or timeout.
- `worker/playback.js`: playback ownership, muted/playing tabs, pending plays, ordered report
  relay, control routing, and one bounded autoplay rescue episode. Removal clears tab-owned state.
- `worker/ratings.js`: one native desired-rating recovery on its original tab, with a deadline,
  revision checks, cancellation, and bounded pending/terminal diagnostics.
- `worker/sensors.js`: isolated/MAIN probes, injection, resync, and idle-only reload cooldowns.
- `worker/setup.js`: connection-only status for the toolbar panel and YouTube Music setup reminder.
- `music_setup.js`: isolated setup reminder on YouTube Music; no playback controls. It opens
  automatically at most twice across Music tabs and browser restarts, using Music's local
  storage and a shared Web Lock. Dismissal keeps that tab quiet for its session. A TC icon
  with an attention badge opens the same reminder on demand; connection hides both.
- `audio_probe.js`: explicitly injected three-second access diagnostic; normal startup does not install it.
- `setup.html`, `setup.js`, `setup.css`: toolbar connection and setup panel.
- `worker/site.js`: public-site connection status and local-player tab activation, with its own
  coalesced open request. It receives no music/library state.
- `worker/radio.js`: saved-playlist companion injection and response delivery. It borrows playback's
  tab selection and owns no tabs, locks, transport listeners, or background timers.
- `content.js`: on-demand in-page fetch, driven by messages from the service worker.
- `site_player.js` / `site_player.css`: the public site's local-player badge and setup dialog.
- `manifest.json`: permissions and wiring for the above.

## Prepare a Chrome Web Store update

Set a version in `manifest.json` higher than the last uploaded version, then run
`scripts/build-extension.sh` from the repository root. Upload the resulting
`dist/tuneconsole-extension-<version>.zip` to the existing store listing.
The package omits the development `key`, README, hidden files, and source maps.

Radio Sessions use a saved playlist in the existing YouTube Music tab. Open
`http://127.0.0.1:8765/#radio` to choose a starting song. Reload the extension after
updating its source so the playlist observer is available.

The old two-deck radio manager and `--enable-radio` preview package were removed in commit
`0e149f3`. The saved-playlist companion above supersedes them and ships in the normal package;
there is no remaining disabled deck flag or separate preview package to preserve.

## Message contracts

These are internal extension/backend messages, not a public web API. The manifest keeps the
MAIN and isolated scripts on Music, and the public-site script on the two TuneConsole hosts.

| Boundary | Messages and response |
| --- | --- |
| MAIN sensor to isolated content | `window.postMessage` carries `__tcSensorVersion: 4` plus `__tcNow` (player/bar metadata, queue, account availability, pause/mute, elapsed position) with `__tcSensorRevision: 10`, or `__tcPlayer` (raw playback event). The isolated receiver accepts messages from the same window and compatible MAIN revisions from 8 onward. `videoVerified` confirms the native player's upload independently of `metadataVerified`. Unverified credits can record listening only for that exact, already known catalog upload; they cannot create tracks or update metadata or ratings. Unknown accounts remain ineligible. |
| MAIN fetch observer to isolated content | `__tcCuration` carries a bounded request body, endpoint, page URL, and account. Ratings include settled `success`/`httpStatus`; other actions report intent. The observer returns the page's original fetch promise unchanged. |
| Isolated content to MAIN | `__tcSensorProbe: {version, id}` requests an immediate sample. MAIN echoes `__tcSensorProbeId` on `__tcNow`. Health and rating commands use this handshake instead of trusting interval timing. |
| Worker to isolated content | `sensor-ping` replies with receiver revision and MAIN version/revision/age, plus bounded `capture` diagnostics from its fresh MAIN probe; `main: false` checks fetch readiness alone. `fetch` carries URL/method/body/auth and replies with status/body/time zone. Both worker and receiver enforce the youtubei allowlist. |
| Worker playback commands to content | `rate` carries the displayed `videoId`, action, optional desired `likeStatus`, expected `brandId`, and `skip`; a fresh MAIN sample must identify the same player, rating target, and requested account before a click. MAIN reads Polymer element/controller data and can identify the rating target independently of the bar's recording credits. The reply waits for the selected native thumb. A missing binding/control returns `retryable` only before any click; for an exact desired state without `skip`, the worker replies `{ok: true, pending: true}` and retries in the background for up to 30 seconds, repairing a missing sensor once. A newer rating cancels recovery; every attempt rechecks the playing song/account. Uncertain delivery and unconfirmed clicks are never replayed. Dislike-and-skip waits for the native thumbs-down, then advances only if the same video is still playing. `next` carries the displayed `videoId` and confirms a changed playing video after a fresh identity check and one native next-button click. `playpause` toggles; `pause` is idempotent; `ensure-playing` replies with actual playback readiness. `refresh-view` defers matching playlist reloads during playback; `resync-now` clears report deduplication and asks MAIN for a fresh snapshot. |
| Isolated content to worker | `play`, metadata-bearing `now-heartbeat`, and raw `pevent`. Playback resolves tab ownership in arrival order before forwarding. Muted/non-owner observations cannot take over controls or persist plays. |
| Worker to local backend | WebSocket `/bridge/ws`: the backend authenticates the browser's extension origin. `play` can persist a verified play/rating; `now-heartbeat` restores presence without persisting a play; `pevent` carries playback/curation observations; `sensor-health` reports counts, recovery, and errors independently of socket connectivity. |
| Local backend to worker | Request `{id, url, method, body}` gets `{id, status, body, timeZone?}`. Only a proven missing receiver allows one retry; an uncertain delivered write is never replayed. Controls are `navigate`, `playpause`, `refresh-view`, and acknowledged `next`/`rate`/`radio-session` (legacy alias `playlist-session-poc`). A `playpause` with an `id` is also acknowledged: it carries the displayed `videoId` and explicit `play`/`pause` action; receiver revision 14 confirms the same player and actual paused state, including clock progress for Play. Legacy unacknowledged controls retain their existing behavior. Now Playing first saves the exact song’s API rating, then sends a native `rate` control if that song is still playing. A dislike then uses the shared skip path: radio reconciles pending playlist writes and prepares a saved successor before skipping; a stale/disabled native button falls back to selecting that exact saved successor. Native acknowledgements are independently checked against observed playback, avoiding a second skip after uncertain delivery. `{ping: 1}` is a keepalive. |
| Worker to MAIN radio companion | Inject on demand, then call `__tcRadioSession.update({playlist, videoIds, refresh})` using `chrome.scripting`. The response reports native playback/page adoption. This boundary has no separate playback ownership or tab creation. |
| Public site to worker | Only `local-player-status` and `open-local-player`, checked against the sender's exact site origin. Replies expose connection/open status, never account, track, or library data. |

Native rating recovery carries a `ratingId` and absolute `expiresAt`. Every attempt stays on
the original tab and checks the configured receiver and MAIN revisions before delivery. The receiver
checks expiry and cancellation after its fresh probe, immediately before a possible click.
`cancel-rating` invalidates an awaiting probe when a newer request, song/account snapshot, tab
closure, or recovery deadline ends the job. A lost reply or an unconfirmed click ends recovery.

Deferred jobs send `rating-reconciliation` frames over the bridge: one `pending`, followed by
one `recovered`, `cancelled`, or `failed`. The backend logs the first and last bounded binding
snapshots, revisions, attempt count, and elapsed time; only `failed` is a warning. Correlated MAIN
probes report renderer/button presence, candidate IDs, and bounded Polymer field names rather
than copying page objects or account configuration. This distinguishes a binding that settles
from persistent missing selectors/data and identifies obsolete receivers. API rating success
remains independent of this native reconciliation.

Exact play capture also supports Music layouts with no player-bar data, watch link, or
video-specific artwork. In that layout, the fresh native rating target must match the active
player's video, and both displayed title and artist must exactly match that same player's
title/author. A rating target alone cannot verify recording credits; missing or conflicting
identifiers/credits remain heartbeats. Normal account, audible playback, ownership, deduplication,
and playlist provenance checks still apply.

The bridge status exposes the sensor's boolean `metadata_verified` separately from presence.
Each sensor-health version entry includes `capture`: verification/account/pause/mute flags,
bar/player credit agreement, candidate counts, and bounded Polymer field names. These probes
omit song/account values and candidate IDs; unanswered probes report no capture snapshot.

Sensor protocol versions describe compatibility; implementation revisions are additive within
that version. The sensor factory defaults omitted revision options for older bootstrap callers.
Compatible newer receivers and MAIN sensors stay installed, and older revisions are refreshed
in place without making a responding tab appear disconnected. Native rating delivery separately
requires the configured minimum receiver and MAIN revisions (or newer), preserving cancellation,
deadline, and player-binding checks.

The isolated receiver has explicit teardown for revision changes. A legacy boolean guard has
anonymous listeners that cannot safely be replaced: playback stays connected while unsupported
native rating commands request a Music page reload. Automatic health repair still never reloads
playing or partially played media. A matching protocol version alone does not prove current code.
`/bridge/status` includes each tab's receiver and MAIN revisions in `sensor_health.versions`
(at most eight entries), so the installed code can be identified without opening DevTools.

The Now Playing card requests confirmation and keeps its displayed state while the command is
pending. A missing/disabled control, blocked playback, or uncertain delivery uses the existing
playback error toast. Sensor health advertises `playbackConfirmation` for the new worker, so it
must return a native acknowledgement, with clock progress for Play. Older store workers retain
their existing paused-state behavior through fresh matching heartbeats, including when elapsed
time is unavailable. No uncertain toggle is replayed.
A manual Pause cancels startup rescue, including delayed work already running in the receiver.
The card opts in with `POST /now-playing/toggle?confirm=true&action=play|pause&videoId=...`;
existing keyboard, retained-player resume, and TV commands keep their current interfaces.

## Extension verification

Run checks serially using the repository's shared pytest lock. A busy lock identifies its owner;
wait with `--test-lock-wait=60` or hand the request to that owner. Do not launch a second test process.

- Live Radio acceptance: start a signed-in session under Home's **On air** tab, then run
  `uv run pytest --test-lock-wait=60 --run-live-radio --live-radio-trace=/tmp/tuneconsole-radio-live.json -s tests/test_radio_live.py`.
  This resumes playback if paused and changes one continuation in the running session. It records the saved playlist,
  native playback and sidebar-refreshed page agreeing on the replacement, checks the current song
  and history survive, then waits for natural playback of the new song. Playback remains running.
  Normal test runs skip this check; mocked native/page tests do not establish this live acceptance.
- Focused behavioral checks: `uv run pytest tests/test_extension_*.py tests/test_playback_rescue.py`. Node executes the actual
  imported worker modules and sensors with explicit Chrome/page doubles. Cases cover both sensor
  worlds, partial injection, invalid/stale samples, repair, update safety, uncertain delivery,
  mute/ownership changes, account/metadata transitions, launch/fetch interleavings, autoplay
  rescue timing/focus/cleanup, and packaging.
- Installed-extension checks: `uv run pytest -m browser tests/browser/test_extension_lifecycle_browser.py tests/browser/test_extension_site_player_browser.py`.
  Install Chromium first with `uv run playwright install chromium`; OpenSSL supplies a temporary
  certificate for the local Music fixture server. The persistent Chromium context loads the unpacked extension,
  uses real MAIN/isolated worlds, native media/tabs, a real WebSocket, and real backend persistence.
  Fixtures cover album-art MediaSession/player bars, separately delayed track/rating bindings,
  unknown accounts, stopped/throttled sensors, reload/reconnect, navigation/removal, multiple-tab
  controls, a dropped socket, and library fetches racing playback. Music's hostname resolves to a
  local TLS server, including the first navigation of an extension-created tab. Google endpoints/private player APIs are fixtures;
  these checks need no signed-in account and do not certify today's live Music markup.
- CI runs `uv run pytest` and then `uv run pytest -m browser` in their respective existing jobs;
  the new tests are included automatically. Each test session remains serial. Missing Node skips
  mocked JS checks locally; CI must install Node. Chromium is required for the browser job.

Before store submission, also check the actual signed-in Music site in headed Chrome: install the
built ZIP unpacked, play a playlist/album, browse another music tab, route pause/rating from the app,
change songs/accounts, verify history and rating attribution, reload the extension while playing,
and confirm playback continues and the app reconnects. Try a background autoplay launch and a
library refresh together; check that only one music tab is created. Edit a playlist during playback,
then pause to verify the deferred refresh. On the public site, check offline setup and the connected
Local Player button. Check the saved-playlist Radio flow separately, including pause/resume and
confirmed playback/page adoption after a continuation change. Browser fixtures cannot establish
current YouTube UI compatibility, OS window focus, or Chrome's real autoplay policy for each user.
