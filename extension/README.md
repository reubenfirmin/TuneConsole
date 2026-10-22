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
It reads Music's native playback store, turns the identified Autoplay switch off, and revisits
the saved playlist after each confirmed change. Once the sidebar page contains the saved tracks,
it follows the current song's native watch endpoint with the current playback position. The
session starts with three songs, then keeps one song saved
ahead; the four continuations on Home are proposals, and selecting one replaces only the
unplayed playlist suffix. Opening Radio reattaches to the current session without restarting it.
It confirms adoption against Music's actual upcoming tracks, separately from the embedded video
player and the browse page. A native navigation request alone does not confirm a direction switch.
Navigation is attempted once per saved version and current song, preserving the paused state.
The observer never dispatches native queue edits or sends a track array to the player.

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
  `BRIDGE_URL` in `background.js` to match.
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

- `background.js`: auto-connecting WebSocket client, allowlist, auth header, relay to the content script.
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
