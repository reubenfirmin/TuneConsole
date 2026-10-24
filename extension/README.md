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
- `music_setup.js`: isolated, dismissible setup reminder on YouTube Music; no playback controls.
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
| MAIN sensor to isolated content | `window.postMessage` carries `__tcSensorVersion: 4` plus `__tcNow` (player/bar metadata, queue, account availability, pause/mute, elapsed position) with `__tcSensorRevision: 8`, or `__tcPlayer` (raw playback event). The isolated receiver accepts messages from the same window, ignores obsolete sensor versions/revisions, and persists neither unknown-account nor unverified metadata snapshots as plays. |
| MAIN fetch observer to isolated content | `__tcCuration` carries a bounded request body, endpoint, page URL, and account. Ratings include settled `success`/`httpStatus`; other actions report intent. The observer returns the page's original fetch promise unchanged. |
| Isolated content to MAIN | `__tcSensorProbe: {version, id}` requests an immediate sample. MAIN echoes `__tcSensorProbeId` on `__tcNow`. Health and rating commands use this handshake instead of trusting interval timing. |
| Worker to isolated content | `sensor-ping` replies with receiver revision and MAIN version/revision/age, plus bounded `capture` diagnostics from its fresh MAIN probe; `main: false` checks fetch readiness alone. `fetch` carries URL/method/body/auth and replies with status/body/time zone. Both worker and receiver enforce the youtubei allowlist. |
| Worker playback commands to content | `rate` carries the displayed `videoId`, action, optional desired `likeStatus`, expected `brandId`, and `skip`; a fresh MAIN sample must identify the same player, rating target, and requested account before a click. MAIN reads Polymer element/controller data and can identify the rating target independently of the bar's recording credits. The reply waits for the selected native thumb. A missing binding/control returns `retryable` only before any click; for an exact desired state without `skip`, the worker replies `{ok: true, pending: true}` and retries in the background for up to 30 seconds, repairing a missing sensor once. A newer rating cancels recovery; every attempt rechecks the playing song/account. Uncertain delivery and unconfirmed clicks are never replayed. Dislike-and-skip waits for the native thumbs-down, then advances only if the same video is still playing. `next` carries the displayed `videoId` and confirms a changed playing video after a fresh identity check and one native next-button click. `playpause` toggles; `pause` is idempotent; `ensure-playing` replies with actual playback readiness. `refresh-view` defers matching playlist reloads during playback; `resync-now` clears report deduplication and asks MAIN for a fresh snapshot. |
| Isolated content to worker | `play`, metadata-bearing `now-heartbeat`, and raw `pevent`. Playback resolves tab ownership in arrival order before forwarding. Muted/non-owner observations cannot take over controls or persist plays. |
| Worker to local backend | WebSocket `/bridge/ws`: the backend authenticates the browser's extension origin. `play` can persist a verified play/rating; `now-heartbeat` restores presence without persisting a play; `pevent` carries playback/curation observations; `sensor-health` reports counts, recovery, and errors independently of socket connectivity. |
| Local backend to worker | Request `{id, url, method, body}` gets `{id, status, body, timeZone?}`. Only a proven missing receiver allows one retry; an uncertain delivered write is never replayed. Controls are `navigate`, `playpause`, `refresh-view`, and acknowledged `next`/`rate`/`radio-session` (legacy alias `playlist-session-poc`). Now Playing first saves the exact song’s API rating, then sends a native `rate` control if that song is still playing. A dislike then uses the shared skip path: radio reconciles pending playlist writes and prepares a saved successor before skipping; a stale/disabled native button falls back to selecting that exact saved successor. Native acknowledgements are independently checked against observed playback, avoiding a second skip after uncertain delivery. `{ping: 1}` is a keepalive. |
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

The isolated receiver now has explicit teardown for future revision changes. A legacy boolean
guard has anonymous listeners that cannot safely be replaced: its health report requests a Music
page reload, and native rating delivery is refused. Automatic health repair still never reloads
playing or partially played media. A matching protocol version alone does not prove current code.
`/bridge/status` includes each tab's receiver and MAIN revisions in `sensor_health.versions`
(at most eight entries), so the installed code can be identified without opening DevTools.

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
