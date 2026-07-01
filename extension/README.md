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
