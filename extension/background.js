// Service worker: holds the persistent WebSocket to the local backend, enforces the endpoint
// allowlist, and relays each allowed request to the content script for an in-page fetch (so
// traffic is visible in the standard music.youtube.com DevTools Network tab). No YouTube fetch
// happens here; this file only decides whether a request is allowed and where to find a tab
// that can make it.

const ORIGIN = "https://music.youtube.com";
const APP_ORIGIN = "http://127.0.0.1:8765";
const SENSOR_VERSION = 4;
let ws = null;

// Keep recurring failures useful without flooding the service worker console.
const warningTimes = new Map();
function warn(message, ...details) {
  const now = Date.now();
  if (warningTimes.has(message) && now - warningTimes.get(message) < 60000) return;
  warningTimes.set(message, now);
  console.warn("[TuneConsole]", message, ...details);
}

function isAllowed(u) {
  try {
    const x = new URL(u);
    return x.origin === "https://music.youtube.com" && x.pathname.startsWith("/youtubei/v1/");
  } catch (e) {
    return false;
  }
}

function isMissingReceiver(error) {
  return String(error && error.message ? error.message : error)
    === "Could not establish connection. Receiving end does not exist.";
}

async function sha1Hex(str) {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(str));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function authHeader() {
  let c = await chrome.cookies.get({ url: ORIGIN, name: "SAPISID" });
  if (!c) c = await chrome.cookies.get({ url: ORIGIN, name: "__Secure-3PAPISID" });
  if (!c) return null;
  const ts = Math.floor(Date.now() / 1000);
  return `SAPISIDHASH ${ts}_${await sha1Hex(`${ts} ${c.value} ${ORIGIN}`)}`;
}

async function ytmTabs() {
  // A newly opened/restored tab can have only pendingUrl. A URL-filtered query misses it,
  // allowing a second launch (or a concurrent library fetch) to create another music tab.
  const tabs = await chrome.tabs.query({});
  return tabs.filter(tab => {
    try { return new URL(tab.pendingUrl || tab.url).origin === ORIGIN; }
    catch (e) { return false; }
  });
}

async function firstYtmTab() {
  const tabs = await ytmTabs();
  return tabs[0] || null;
}

// Playback launches and library fetches must make their find/create decision under the same
// lock. Release it before waiting for document readiness so playback can navigate a starting tab.
let tabCoordination = Promise.resolve();
function coordinateYtmTabs(operation) {
  const result = tabCoordination.then(operation);
  tabCoordination = result.catch(() => {});
  return result;
}

// A tab opened before the extension loaded never got the declared content script. Inject it on
// demand. content.js guards against double-registration, so injecting an already-present script is
// a no-op.
async function inject(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    // The MAIN-world now-playing companion (reads the page's MediaSession).
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", files: ["nowplaying_main.js"] });
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", files: ["observer_main.js"] });
    return { ok: true, error: "" };
  } catch (e) {
    // Injection failures used to disappear here, leaving the backend "connected" with no tab
    // sensor and no explanation. Preserve the failure for the health report and DevTools.
    const error = String(e && e.message ? e.message : e);
    warn("YTM sensor injection failed for tab", tabId, error);
    return { ok: false, error };
  }
}
async function injectAllYtmTabs() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  for (const t of tabs) await inject(t.id);
}

// Ask each YouTube Music tab to re-emit what's playing right now (used on (re)connect, since the
// backend forgets now_playing when the socket drops and the content script otherwise dedups it away).
async function resyncNowPlaying() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  for (const t of tabs) { try { await chrome.tabs.sendMessage(t.id, { type: "resync-now" }); } catch (e) {} }
}

async function pingSensor(tabId, requireMain = true) {
  try {
    const reply = await chrome.tabs.sendMessage(tabId, { type: "sensor-ping", main: requireMain });
    if (!reply || !reply.ok) return { ok: false, error: "invalid sensor reply" };
    if (requireMain && (reply.mainVersion !== SENSOR_VERSION ||
        typeof reply.mainAgeMs !== "number" || !Number.isFinite(reply.mainAgeMs) ||
        reply.mainAgeMs < 0 || reply.mainAgeMs >= 10000)) {
      return { ok: false, reply, error: "MAIN sensor missing, outdated, or stale" };
    }
    return { ok: true, reply };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
}

// A failed reinjection may need a real navigation boundary to recover. Reload only after a separate
// media probe confirms it is idle; an update must not cut off music if sensor replacement fails.
const SENSOR_RELOAD_COOLDOWN_MS = 60000;
const sensorReloadedAt = new Map();
async function reloadUnresponsiveSensor(tabId) {
  const last = sensorReloadedAt.get(tabId) || 0;
  if (Date.now() - last < SENSOR_RELOAD_COOLDOWN_MS) return false;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab.audible) return false;
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => Array.from(document.querySelectorAll("video, audio"))
        .some(media => !media.ended && (!media.paused || media.currentTime > 0)),
    });
    // A missing result is unknown, so defer repair until a later probe or a user navigation.
    if (results[0]?.result !== false) return false;
    sensorReloadedAt.set(tabId, Date.now());
    await suppressBeforeUnload(tabId);
    await chrome.tabs.reload(tabId);
    return true;
  } catch (e) {
    warn("Could not reload YTM tab", tabId, e);
    return false;
  }
}

// Observable + self-healing tab boundary. A live WebSocket proves only the service worker is alive;
// this independently proves that each YTM tab has responding isolated and MAIN sensors. A failed probe gets
// one reinjection and retry, and the result is reported to /bridge/status without touching play data.
async function probeSensorHealth() {
  let tabs = [], responding = 0, reinjected = 0, reloading = 0, errors = [];
  try { tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" }); }
  catch (e) { errors.push(String(e && e.message ? e.message : e)); }
  for (const tab of tabs) {
    let probe = await pingSensor(tab.id);
    if (!probe.ok) {
      const installed = await inject(tab.id);
      if (installed.ok) reinjected += 1;
      else errors.push(installed.error);
      probe = installed.ok ? await pingSensor(tab.id) : probe;
    }
    if (probe.ok) responding += 1;
    else if (await reloadUnresponsiveSensor(tab.id)) reloading += 1;
    else errors.push(`tab ${tab.id}: ${probe.error || "sensor did not answer"}`);
  }
  const report = { type: "sensor-health", ytmTabs: tabs.length, respondingTabs: responding,
                   reinjectedTabs: reinjected, healthy: tabs.length > 0 && responding === tabs.length,
                   recovering: reloading > 0, reloadedTabs: reloading,
                   error: errors.filter(Boolean).join("; ").slice(0, 500) };
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(report)); } catch (e) {}
  }
  return report;
}

// Note: rate/playpause below intentionally stay no-ops when there is no YTM tab (via
// pickYtmTab returning null). A rate/playpause control is meaningless without a tab
// already playing something, so unlike the fetch path there is nothing useful to open a tab for.

// Playback ownership survives queue changes, including YouTube autoplay outside our playlists.
// Browsing another music tab must not redirect controls away from the tab making sound.
const playingTabs = new Set();
const mediaMutedTabs = new Set();
const pendingPlays = new Map();
let playbackTabId = null;
async function pickYtmTab() {
  const tabs = await ytmTabs();
  if (!tabs.length) return null;
  const unmuted = tabs.filter(t => !t.mutedInfo?.muted && !mediaMutedTabs.has(t.id));
  const playing = unmuted.find(t => t.audible) ||
    unmuted.find(t => t.id === playbackTabId && playingTabs.has(t.id)) ||
    unmuted.find(t => playingTabs.has(t.id)) ||
    unmuted.find(t => t.id === playbackTabId) || tabs.find(t => t.id === playbackTabId);
  if (playing) return playing;
  const active = tabs.find((t) => t.active);
  if (active) return active;
  return tabs.slice().sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
}

// Land a backend "navigate" request in the EXISTING YouTube Music tab, not a new one. Scoped to
// #101 Playback rescue. Swapping the tab in the background is the right default, but it often will
// not start playing: Chrome gates unmuted playback on a user gesture or a high media-engagement
// score, and a tab that was never foregrounded regularly loses that bet. The swap then lands
// silently and nothing happens. So watch the navigated tab for a REAL playing frame, and if none
// turns up, show the tab briefly (which is what actually gets YouTube Music going) and put the owner
// straight back where they were.
//
// Return to TuneConsole after the nudge even if the playing report is delayed or autoplay remains
// blocked. For other starting tabs, keep the existing return-on-success behaviour.
const RESCUE_WAIT_MS = 2500;   // let a normal navigate + autoplay report in first, so the common case never flickers
const RESCUE_DWELL_MS = 600;   // minimum foreground time; a paused/loading player gets longer
const RESCUE_RETRY_MS = 500;
const RESCUE_TIMEOUT_MS = 6000;
let rescue = null;

function cancelPlaybackRescue() {
  if (!rescue) return;
  try { clearTimeout(rescue.timer); } catch (e) {}
  try { clearTimeout(rescue.retryTimer); } catch (e) {}
  rescue = null;
}

function armPlaybackRescue(tabId) {
  cancelPlaybackRescue();          // a newer play supersedes any episode still in flight
  playingTabs.delete(tabId);       // the navigate wipes the old document: assume silence until told otherwise
  mediaMutedTabs.delete(tabId);
  pendingPlays.delete(tabId);
  rescue = { tabId, foregrounded: false, timer: setTimeout(() => runPlaybackRescue(tabId), RESCUE_WAIT_MS) };
}

async function runPlaybackRescue(tabId) {
  const episode = rescue;
  if (!episode || episode.tabId !== tabId || episode.foregrounded) return;
  let returnTab = null;
  try {
    // lastFocusedWindow, NOT currentWindow: a service worker has no window of its own, so
    // "currentWindow" has nothing to resolve against here. Capture the tab immediately before the
    // nudge, since the owner may have changed tabs during the initial autoplay wait.
    const [cur] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (cur && cur.id !== tabId) returnTab = cur;
  } catch (e) {}
  if (rescue !== episode) return;
  episode.foregrounded = true;
  try {
    const shown = await chrome.tabs.update(tabId, { active: true });
    if (rescue !== episode) return;
    if (returnTab && shown.windowId != null && shown.windowId !== returnTab.windowId) {
      await chrome.windows.update(shown.windowId, { focused: true });
    }
  } catch (e) {
    if (rescue === episode) cancelPlaybackRescue();
    return;                                        // tab closed under us: nothing to rescue
  }
  if (rescue !== episode) return;
  episode.returnTab = returnTab;
  episode.startedAt = Date.now();
  // Bound the entire episode, including a message to a navigating/unresponsive document. One
  // fire-and-forget message followed by a fixed dwell lost requests while YTM was still loading.
  episode.timer = setTimeout(() => {
    warn("Playback nudge timed out", tabId);
    return finishPlaybackRescue(episode);
  }, RESCUE_TIMEOUT_MS);
  attemptPlaybackRescue(episode);
}

async function attemptPlaybackRescue(episode) {
  if (rescue !== episode || episode.finishing || episode.confirmed) return;
  episode.retryTimer = setTimeout(() => attemptPlaybackRescue(episode), RESCUE_RETRY_MS);
  if (episode.inFlight) return;
  episode.inFlight = true;
  try {
    const reply = await chrome.tabs.sendMessage(episode.tabId, { type: "ensure-playing" });
    if (rescue !== episode || episode.finishing || !reply || !reply.playing) return;
    // content.js confirms an unpaused media element with playable data. Delivery alone is not
    // success: an absent video, pending play(), or blocked autoplay must keep the retry alive.
    playingTabs.add(episode.tabId);
    episode.confirmed = true;
    clearTimeout(episode.retryTimer);
    clearTimeout(episode.timer);
    const remaining = Math.max(0, RESCUE_DWELL_MS - (Date.now() - episode.startedAt));
    episode.timer = setTimeout(() => finishPlaybackRescue(episode), remaining);
  } catch (e) {
    // Extension reloads and document swaps can leave an open YTM tab without a receiver. Install
    // the guarded scripts again, then retry; never reload the user's music tab to repair this.
    if (rescue === episode && !episode.finishing) await inject(episode.tabId);
  } finally {
    episode.inFlight = false;
  }
}

async function finishPlaybackRescue(episode) {
  if (rescue !== episode || episode.finishing) return;
  episode.finishing = true;
  clearTimeout(episode.timer);
  clearTimeout(episode.retryTimer);
  const { tabId, returnTab } = episode;
  try {
    const fromApp = returnTab && (returnTab.url || "").startsWith(APP_ORIGIN + "/");
    if (!returnTab || (!fromApp && !playingTabs.has(tabId))) return;
    const [cur] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    // A manual tab/window switch or a newer request takes precedence over our temporary nudge.
    if (rescue !== episode || !cur || cur.id !== tabId) return;
    await chrome.tabs.update(returnTab.id, { active: true });
    if (rescue === episode && returnTab.windowId != null && returnTab.windowId !== cur.windowId) {
      await chrome.windows.update(returnTab.windowId, { focused: true });
    }
  } catch (e) {}   // the original tab/window may have closed during the nudge
  finally { if (rescue === episode) cancelPlaybackRescue(); }
}

// music.youtube.com so the backend can only ever point our own tab at YouTube Music.
async function navigateYtmTab(url) {
  try {
    if (new URL(url).origin !== ORIGIN) return;
  } catch (e) {
    return;
  }
  // Share tab creation with fetch startup as well as other playback launches.
  return coordinateYtmTabs(async () => {
    const tab = await pickYtmTab();
    if (tab) {
      // Reuse the existing tab in the background so you stay on TuneConsole.
      try {
        await suppressBeforeUnload(tab.id);
        await chrome.tabs.update(tab.id, { url });
        playbackTabId = tab.id;
        armPlaybackRescue(tab.id);
        await pauseOtherYtmTabs(tab.id);
      } catch (e) {
        warn("Navigate update failed", e);
      }
    } else {
      // A brand-new background tab needs the same autoplay rescue as a reused one.
      try {
        const made = await chrome.tabs.create({ url, active: false });
        if (made && made.id != null) {
          playbackTabId = made.id;
          armPlaybackRescue(made.id);
          await pauseOtherYtmTabs(made.id);
        }
      } catch (e) {}
    }
  }).catch(e => warn("Navigate failed", e));
}

async function pauseOtherYtmTabs(keepId) {
  const tabs = await ytmTabs();
  await Promise.all(tabs.filter(t => t.id !== keepId).map(async tab => {
    await driveTab(tab.id, "pause"); // idempotent: never resumes an already paused player
    playingTabs.delete(tab.id);
  }));
}

// YouTube Music registers a beforeunload handler that pops "Reload site? Changes you made may not be
// saved" when the tab navigates. Neutralize it in the page (MAIN world) right before we swap the tab,
// so the swap is seamless. A capturing listener that stops propagation prevents the page's own
// handler from setting returnValue, and nulling onbeforeunload covers the property-style handler.
async function suppressBeforeUnload(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        try { window.onbeforeunload = null; } catch (e) {}
        window.addEventListener("beforeunload", (e) => { e.stopImmediatePropagation(); }, true);
      },
    });
  } catch (e) {}
}

async function sendFetch(tabId, frame, auth) {
  return chrome.tabs.sendMessage(tabId, {
    type: "fetch", url: frame.url, method: frame.method, body: frame.body, auth,
  });
}

// Ask the content script to like/dislike the current track by driving YTM's own player-bar control.
async function rateInYtmTab(action, videoId, likeStatus) {
  if (!videoId) return {ok: false, error: "Displayed song is missing"};
  const tab = await pickYtmTab();
  if (!tab) return {ok: false, error: "No music player is available"};
  try {
    return await chrome.tabs.sendMessage(tab.id, { type: "rate", action, videoId, likeStatus }) || {ok: false};
  } catch (e) {
    if (!isMissingReceiver(e)) return {ok: false, error: String(e)}; // never retry a possible click
    await inject(tab.id);
    try { return await chrome.tabs.sendMessage(tab.id, { type: "rate", action, videoId, likeStatus }) || {ok: false}; }
    catch (e2) { return {ok: false, error: String(e2)}; }
  }
}

// Ask the content script to play/pause the current track by driving YTM's own player-bar control.
async function playPauseInYtmTab() {
  const tab = await pickYtmTab();
  if (!tab) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "playpause" });
  } catch (e) {
    if (!isMissingReceiver(e)) return; // a second click would undo a delivered toggle
    await inject(tab.id);
    try { await chrome.tabs.sendMessage(tab.id, { type: "playpause" }); } catch (e2) {}
  }
}

// Ask the content script to refresh the YTM tab's view so edits made in TuneConsole (rename, art,
// tracklist, etc.) show up promptly without ever interrupting playback. The content script owns the
// "is it safe to reload" decision (it checks the URL and playback state); this just delivers the ask.
async function refreshViewInYtmTab(playlist) {
  // The edited playlist may be open in a browsing tab while another tab owns playback.
  for (const tab of await ytmTabs()) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "refresh-view", playlist });
    } catch (e) {
      await inject(tab.id);
      try { await chrome.tabs.sendMessage(tab.id, { type: "refresh-view", playlist }); } catch (e2) {}
    }
  }
}

// Observe Music's playback state and revisit its saved-playlist watch endpoint.
async function playlistSessionInYtmTab(frame) {
  const tab = await pickYtmTab();
  if (!tab) return {ok: false, error: "No YouTube Music tab is open."};
  await chrome.scripting.executeScript({
    target: {tabId: tab.id}, world: "MAIN", files: ["radio_session.js"],
  });
  const results = await chrome.scripting.executeScript({
    target: {tabId: tab.id}, world: "MAIN",
    func: frame => window.__tcRadioSession.update(frame),
    args: [{playlist: frame.playlist, videoIds: frame.videoIds, refresh: !!frame.refresh}],
  });
  return results[0]?.result || {ok: false, error: "The playlist observer returned no result."};
}

// Same inject-retry pattern used throughout this file: send once, and on failure inject the content
// script then retry once.
// Returns whether the message was actually delivered.
async function driveTab(tabId, type, extra) {
  try {
    await chrome.tabs.sendMessage(tabId, Object.assign({ type }, extra || {}));
    return true;
  } catch (e) {
    await inject(tabId);
    try {
      await chrome.tabs.sendMessage(tabId, Object.assign({ type }, extra || {}));
      return true;
    } catch (e2) {
      return false;
    }
  }
}

// A closed tab is neither playing nor rescuable.
chrome.tabs.onRemoved.addListener((tabId) => {
  playingTabs.delete(tabId);
  mediaMutedTabs.delete(tabId);
  pendingPlays.delete(tabId);
  if (playbackTabId === tabId) playbackTabId = null;
  if (rescue && rescue.tabId === tabId) cancelPlaybackRescue();
});

// Bound navigation waits when a page never finishes loading or its tab closes.
function waitForTabComplete(tabId, budgetMs) {
  budgetMs = budgetMs == null ? 30000 : budgetMs;
  return new Promise((resolve) => {
    let done = false;
    let timer = null;
    const finish = (ok) => {
      if (done) return;
      done = true;
      if (timer != null) clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve(ok);
    };
    const onUpdated = (id, info) => {
      if (id === tabId && info.status === "complete") finish(true);
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    timer = setTimeout(() => finish(false), budgetMs);
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) { finish(false); return; }
      if (tab && tab.status === "complete" && !tab.pendingUrl) finish(true);
    });
  });
}

// Readiness checks must not reset play deduplication or re-emit persisted play reports.
async function pingContentScript(tabId) {
  return (await pingSensor(tabId, false)).ok;
}

// New and already-loading tabs share the same readiness boundary. Keep the entire wait within
// the fetch startup budget, including the initial document load.
async function waitForYtmTabReady(tab) {
  const deadline = Date.now() + 15000;
  if (!await waitForTabComplete(tab.id, 15000)) return null;
  await inject(tab.id);
  while (Date.now() < deadline) {
    if (await pingContentScript(tab.id)) return tab;
    await inject(tab.id);
    await new Promise((r) => setTimeout(r, 300));
  }
  return null;
}

async function createAndWaitForYtmTab() {
  const selected = await coordinateYtmTabs(async () => {
    const existing = await firstYtmTab();
    if (existing) return { tab: existing, created: false };
    try {
      return { tab: await chrome.tabs.create({ url: `${ORIGIN}/`, active: false }), created: true };
    } catch (e) {
      return null;
    }
  });
  if (!selected || !selected.tab) return null;
  const { tab, created } = selected;
  return created || tab.status === "loading" || tab.pendingUrl ? waitForYtmTabReady(tab) : tab;
}

// Install the shared promise before the first tab query. A concurrent fetch must wait even after
// Chrome exposes the newly created tab, while its document and sensor are still loading.
let ensureTabPromise = null;
function ensureYtmTab() {
  if (!ensureTabPromise) {
    ensureTabPromise = createAndWaitForYtmTab().finally(() => { ensureTabPromise = null; });
  }
  return ensureTabPromise;
}

async function handleFrame(frame) {
  // Allowlist is the load-bearing control: refuse anything not a youtubei call.
  if (!isAllowed(frame.url)) {
    return { id: frame.id, status: 0, body: JSON.stringify({ error: "blocked by allowlist" }) };
  }
  // No open YTM tab is no longer a dead end: open one in the background and wait for it, same as
  // navigateYtmTab already does for the navigate control.
  const tab = await ensureYtmTab();
  if (!tab) return { id: frame.id, status: 0, body: JSON.stringify({ error: "open music.youtube.com" }) };
  const auth = await authHeader();
  if (!auth) return { id: frame.id, status: 0, body: JSON.stringify({ error: "not signed in" }) };
  // Fetch runs in the page (content script) so it appears in the normal Network tab.
  let res;
  try {
    res = await sendFetch(tab.id, frame, auth);
    if (!res) throw new Error("no response from content script");
  } catch (e) {
    // Only a missing receiver proves the request never reached fetch(). A lost reply may follow
    // a successful mutation; replaying it could add a track twice or undo a toggle.
    const failed = error => ({
      id: frame.id, status: 0,
      body: JSON.stringify({ error: isMissingReceiver(error)
        ? "content script not ready, reload the music.youtube.com tab"
        : "response lost; the request may have completed. Check YouTube Music before retrying." }),
    });
    if (!isMissingReceiver(e)) return failed(e);
    const installed = await inject(tab.id);
    if (!installed.ok) return failed(e);
    try {
      res = await sendFetch(tab.id, frame, auth);
      if (!res) throw new Error("no response from content script");
    } catch (e2) {
      return failed(e2);
    }
  }
  return { id: frame.id, status: res.status, body: res.body, timeZone: res.timeZone };
}

// The public site gets only the local connection status, never playback or library data.
const SITE_TAB_GLOBS = ["https://tuneconsole.com/*", "https://www.tuneconsole.com/*"];
function isTuneConsoleSite(url) {
  try {
    return ["https://tuneconsole.com", "https://www.tuneconsole.com"].includes(new URL(url).origin);
  } catch (e) {
    return false;
  }
}
function localPlayerStatus() {
  return { type: "local-player-status", connected: !!ws && ws.readyState === WebSocket.OPEN };
}
function isLocalPlayerUrl(url) {
  try {
    const candidate = new URL(url), app = new URL(APP_ORIGIN);
    return candidate.protocol === app.protocol && candidate.port === app.port
      && [app.hostname, "localhost"].includes(candidate.hostname);
  } catch (e) {
    return false;
  }
}
async function localPlayerTabs() {
  // Both loopback names reach the same app. Inspect pendingUrl too, so a tab still loading
  // (or restored/discarded by the browser) is reused without navigating away from its page.
  const tabs = await chrome.tabs.query({});
  return tabs.filter(tab => isLocalPlayerUrl(tab.pendingUrl || tab.url));
}
let localPlayerOpenPromise = null;
function openLocalPlayer() {
  if (!localPlayerStatus().connected) return Promise.resolve({ opened: false, connected: false });
  if (!localPlayerOpenPromise) {
    localPlayerOpenPromise = (async () => {
      const tabs = await localPlayerTabs();
      const tab = tabs.find(t => t.active) || tabs[0];
      if (tab) {
        await chrome.tabs.update(tab.id, { active: true });
        if (tab.windowId != null) {
          try { await chrome.windows.update(tab.windowId, { focused: true }); } catch (e) {}
        }
      } else {
        await chrome.tabs.create({ url: APP_ORIGIN + "/", active: true });
      }
      return { opened: true, connected: true };
    })().finally(() => { localPlayerOpenPromise = null; });
  }
  return localPlayerOpenPromise;
}
async function publishLocalPlayerStatus() {
  let tabs;
  try { tabs = await chrome.tabs.query({ url: SITE_TAB_GLOBS }); }
  catch (e) { return; }
  await Promise.all(tabs.map(async (tab) => {
    if (!isTuneConsoleSite(tab.url)) return;
    try {
      await chrome.tabs.sendMessage(tab.id, localPlayerStatus());
    } catch (e) {
      // Include site tabs that were already open when the extension was installed or reloaded.
      try {
        await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ["site_player.css"] });
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["site_player.js"] });
        await chrome.tabs.sendMessage(tab.id, localPlayerStatus());
      } catch (e) {} // The tab may have closed or navigated away.
    }
  }));
}
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !isTuneConsoleSite(sender.url)) return;
  if (msg.type === "local-player-status") {
    sendResponse(localPlayerStatus());
  } else if (msg.type === "open-local-player") {
    openLocalPlayer().then(sendResponse).catch(() => {
      sendResponse({ opened: false, connected: localPlayerStatus().connected });
    });
    return true;
  }
});

// The backend authenticates us by our extension origin, so there is nothing to configure: we just
// connect to the local bridge on the default port. The backend binds 127.0.0.1 on this same port.
const BRIDGE_URL = "ws://127.0.0.1:8765/bridge/ws";

function connect() {
  if (ws && ws.readyState !== WebSocket.CLOSED) return;
  const sock = new WebSocket(BRIDGE_URL);
  ws = sock;
  sock.onopen = async () => {
    publishLocalPlayerStatus();
    await probeSensorHealth();  // prove/recover the tab sensor; reports failures instead of hiding them
    resyncNowPlaying();         // backend cleared now_playing on the last disconnect; re-emit the current track
  };
  sock.onmessage = async (ev) => {
    const frame = JSON.parse(ev.data);
    if (frame && frame.ping) return; // keepalive from the backend; receiving it keeps this SW awake
    if (frame && frame.type === "navigate") { navigateYtmTab(frame.url); return; }
    if (frame && frame.type === "rate") {
      let result;
      try { result = await rateInYtmTab(frame.action, frame.videoId, frame.likeStatus); }
      catch (e) { result = {ok: false, error: String(e)}; }
      try { sock.send(JSON.stringify({id: frame.id, status: 200, body: JSON.stringify(result)})); } catch (e) {}
      return;
    }
    if (frame && frame.type === "playpause") { playPauseInYtmTab(); return; }
    if (frame && frame.type === "refresh-view") { refreshViewInYtmTab(frame.playlist); return; }
    if (frame && (frame.type === "radio-session" || frame.type === "playlist-session-poc")) {
      let result;
      try { result = await playlistSessionInYtmTab(frame); }
      catch (e) { result = {ok: false, error: String(e)}; }
      try { sock.send(JSON.stringify({id: frame.id, status: 200, body: JSON.stringify(result)})); } catch (e) {}
      return;
    }
    const reply = await handleFrame(frame);
    try { sock.send(JSON.stringify(reply)); } catch (e) {}
  };
  sock.onclose = () => {
    if (ws === sock) {
      ws = null;
      publishLocalPlayerStatus();
      setTimeout(connect, 3000);
    }
  };
  sock.onerror = () => { try { sock.close(); } catch (e) {} };
}

// Relay now-playing and playback observations from the content script to the backend.
async function relayPlaybackMessage(msg, sender) {
  if (!msg) return;
  // #101 Playback rescue: track which tabs are actually PLAYING (content.js reports paused off the
  // real <video>, so a paused play frame is not playback), and stand the rescue down the moment the
  // navigated tab reports real sound. That is the "if needed" in the issue: the switch only ever
  // happens when the background swap genuinely failed to start.
  const srcTabId = sender && sender.tab && sender.tab.id;
  if ((msg.type === "play" || msg.type === "now-heartbeat") && srcTabId != null) {
    msg = Object.assign({}, msg, {muted: !!msg.muted || msg.volume === 0});
    if (msg.type === "play") pendingPlays.set(srcTabId, msg);
    if (msg.muted) mediaMutedTabs.add(srcTabId);
    else mediaMutedTabs.delete(srcTabId);
    if (msg.paused || msg.muted) {
      playingTabs.delete(srcTabId);
    } else {
      playingTabs.add(srcTabId);
    }
    // Use the same current tab/mute information as rate and play/pause controls. Unpaused alone
    // does not establish ownership: another tab may be audible while this one is tab/media-muted.
    const owner = await pickYtmTab();
    playbackTabId = owner ? owner.id : null;
    if (srcTabId !== playbackTabId) return;
    // A selected owner can still be silent (including the sole music tab). Keep presence and
    // the pending play, but only promote it when both Chrome and the media element are audible.
    msg = Object.assign({}, msg, {tabMuted: !!owner.mutedInfo?.muted,
      muted: !!msg.muted || msg.volume === 0});
    // Chrome's audible flag can lag a pause/track handoff. Keep a rejected play until this tab
    // actually owns playback, rather than losing it forever to the content sensor's dedup key.
    const pending = pendingPlays.get(srcTabId);
    if (msg.type === "now-heartbeat" && !msg.paused && !msg.muted && !owner.mutedInfo?.muted &&
        msg.brandKnown && msg.metadataVerified !== false && pending &&
        msg.videoId === pending.videoId && msg.brandId === pending.brandId) {
      msg = Object.assign({}, pending, msg, { type: "play" });
    }
    if (!msg.paused && !msg.muted && !owner.mutedInfo?.muted &&
        rescue && rescue.tabId === srcTabId && !rescue.foregrounded) cancelPlaybackRescue();
    if (msg.type === "play" && (msg.muted || msg.tabMuted || msg.paused)) {
      msg = Object.assign({}, msg, {type: "now-heartbeat"});
    }
  }
  if (msg.type === "pevent" && ["state", "bye"].includes(msg.kind)) {
    const owner = await pickYtmTab();
    if (owner) playbackTabId = owner.id;
    if (playbackTabId != null && srcTabId !== playbackTabId) return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (msg.type === "play") {
    ws.send(JSON.stringify({ type: "play", title: msg.title, artist: msg.artist,
      metadataVerified: msg.metadataVerified !== false,
      thumbnail: msg.thumbnail, likeStatus: msg.likeStatus, videoId: msg.videoId,
      playlist: msg.playlist || "", albumBrowseId: msg.albumBrowseId || "",
      brandId: msg.brandId || "", paused: !!msg.paused, muted: !!msg.muted,
      tabMuted: !!msg.tabMuted, timeZone: msg.timeZone }));
    pendingPlays.delete(srcTabId);
  } else if (msg.type === "now-heartbeat") {
    // Metadata makes the non-persisting heartbeat able to restore a card that aged out while Chrome
    // throttled the YTM page (commonly during an audio-device switch).
    ws.send(JSON.stringify({ type: "now-heartbeat", title: msg.title, artist: msg.artist,
      metadataVerified: msg.metadataVerified !== false,
      thumbnail: msg.thumbnail, likeStatus: msg.likeStatus, videoId: msg.videoId,
      playlist: msg.playlist || "", albumBrowseId: msg.albumBrowseId || "", paused: !!msg.paused,
      muted: !!msg.muted, tabMuted: !!msg.tabMuted }));
  } else if (msg.type === "pevent") {
    ws.send(JSON.stringify(msg));
  }
}

// Tab queries are asynchronous; serialize reports so an older snapshot cannot overtake a newer
// play/pause or account change while ownership is being resolved.
let playbackRelay = Promise.resolve();
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || !["play", "now-heartbeat", "pevent"].includes(msg.type)) return;
  playbackRelay = playbackRelay.then(() => relayPlaybackMessage(msg, sender))
    .catch(error => warn("Playback relay failed", error));
});

// Belt and suspenders: an alarm wakes the service worker periodically so it reconnects if the
// socket ever dropped while it was suspended (the backend ping keeps it alive while connected).
chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(() => { connect(); probeSensorHealth(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
// Refresh the extension's sensors in place so a store update does not interrupt music.
chrome.runtime.onInstalled.addListener(injectAllYtmTabs);
chrome.runtime.onInstalled.addListener(publishLocalPlayerStatus);
chrome.runtime.onStartup.addListener(publishLocalPlayerStatus);
connect();

// Manual app tabs can hold different views or unfinished edits. openLocalPlayer() already reuses
// an existing tab for extension launches; leave tabs opened by the user alone.
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url && tab.url.startsWith("https://music.youtube.com/")) {
    probeSensorHealth();
  }
});
