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

- The service worker console (`chrome://extensions` > this extension > "service worker") logs
  "connected" once the WebSocket to the backend is up.
- When the app makes a request (for example, a sync), the request appears in the YTM tab's
  DevTools **Network** tab, filtered for `youtubei`, right alongside YouTube Music's own API
  calls. That visibility is the whole point: nothing is hidden in the service worker.
- Requests outside the allowlist (anything that is not a `youtubei/v1/` call) are refused with a
  "blocked by allowlist" error instead of being sent.

## What the extension observes and sends

The extension acts as a sensor for your listening and curation behavior on YouTube Music, capturing
both playback events (play/pause, track changes, completion) and curation actions (likes, playlist
edits, follows). All events land in the local database; nothing leaves your machine.

### Playback events

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

## If it fails

Check the service worker console for the reason:
- No connection: confirm the app is running on `127.0.0.1:8765`. If you changed the port, update
  `BRIDGE_URL` in `background.js` to match.
- "open music.youtube.com": no matching tab was found; open one and keep it open.
- "not signed in": the `SAPISID` (or `__Secure-3PAPISID`) cookie was not found; sign in to
  `music.youtube.com` and retry.
- "blocked by allowlist": the backend asked for a URL outside `youtubei/v1/`; this is expected
  behavior, not a bug.

## Files

- `background.js`: auto-connecting WebSocket client, allowlist, auth header, relay to the content script.
- `content.js`: on-demand in-page fetch, driven by messages from the service worker.
- `manifest.json`: permissions and wiring for the above.
