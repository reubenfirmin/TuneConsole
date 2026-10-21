// Service worker: holds the persistent WebSocket to the local backend, enforces the endpoint
// allowlist, and relays each allowed request to the content script for an in-page fetch (so
// traffic is visible in the standard music.youtube.com DevTools Network tab). No YouTube fetch
// happens here; this file only decides whether a request is allowed and where to find a tab
// that can make it.

const ORIGIN = "https://music.youtube.com";
const APP_ORIGIN = "http://127.0.0.1:8765";
// Build-time flag: scripts/build-extension.sh --enable-radio opts into the unreleased feature.
const RADIO_ENABLED = false;
const SENSOR_VERSION = 4;
const RADIO_CONTROL_TYPES = new Set([
  "radio-prime", "deck-start", "deck-navigate", "deck-boundary-config", "deck-toggle", "deck-stop",
]);
let ws = null;

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
    console.warn("[TuneConsole] YTM sensor injection failed for tab", tabId, error);
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
    console.warn("[TuneConsole] reloading YTM tab to restore its sensor", tabId);
    return true;
  } catch (e) {
    console.warn("[TuneConsole] could not reload YTM tab", tabId, e);
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

// Note: rate/playpause/radio-prime below intentionally stay no-ops when there is no YTM tab (via
// pickYtmTab returning null). A rate/playpause/radio-prime control is meaningless without a tab
// already playing something, so unlike the fetch path there is nothing useful to open a tab for.

// Playback ownership survives queue changes, including YouTube autoplay outside our playlists.
// Browsing another music tab must not redirect controls away from the tab making sound.
const playingTabs = new Set();
const mediaMutedTabs = new Set();
const pendingPlays = new Map();
let playbackTabId = null;
async function pickYtmTab() {
  const tabs = (await ytmTabs()).filter(t => t.id !== standbyTabId);
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
    console.warn("[TuneConsole] playback nudge timed out", tabId);
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
        console.log("[TuneConsole] navigated existing YTM tab (background)", tab.id, "->", url);
      } catch (e) {
        console.warn("[TuneConsole] navigate update failed:", e);
      }
    } else {
      console.log("[TuneConsole] no existing YTM tab, opening one in the background");
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
  }).catch(e => console.warn("[TuneConsole] navigate failed:", e));
}

async function pauseOtherYtmTabs(keepId) {
  const tabs = await ytmTabs();
  await Promise.all(tabs.filter(t => t.id !== keepId).map(async tab => {
    await driveTab(tab.id, "deck-pause"); // idempotent: never resumes an already paused player
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
async function rateInYtmTab(action, videoId) {
  if (!videoId) return {ok: false, error: "Displayed song is missing"};
  const tab = await pickYtmTab();
  if (!tab) return {ok: false, error: "No music player is available"};
  try {
    return await chrome.tabs.sendMessage(tab.id, { type: "rate", action, videoId }) || {ok: false};
  } catch (e) {
    if (!isMissingReceiver(e)) return {ok: false, error: String(e)}; // never retry a possible click
    await inject(tab.id);
    try { return await chrome.tabs.sendMessage(tab.id, { type: "rate", action, videoId }) || {ok: false}; }
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

// Ask the content script to remember the next radio track's URL, so it can hand off to it the instant
// the current track ends (beating YouTube Music autoplay). Same shape as the other tab-scoped asks.
async function primeRadioInYtmTab(url) {
  const tab = await pickYtmTab();
  if (!tab) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "radio-prime", url });
  } catch (e) {
    await inject(tab.id);
    try { await chrome.tabs.sendMessage(tab.id, { type: "radio-prime", url }); } catch (e2) {}
  }
}

// #93 v3 dual-deck: the deck manager (T7j). Owns a dedicated, compact, unfocused radio WINDOW whose
// two tabs are the live and standby decks -- a pre-build physics probe (since removed) verified the
// one mechanism that makes this work: play() while the tab is still muted, in the ACTIVE
// tab of a visible (even if unfocused) window, then unmute at the tab level. Fails open throughout:
// any failure to stand up the window/tabs reports deck-ready {fallback:true} and the backend stays on
// its existing single-tab v2 path; nothing here can block the fetch/rate/playpause/navigate paths.
let deckWindowId = null, liveTabId = null, standbyTabId = null, deckGen = null, lastDeckEpoch = null;
// Per-deck boundary vids the background itself remembers (C2, final review): a deck-navigate is a
// hard reload that wipes the content script's own boundaryVid, and the radio-boundary message sent
// alongside it races the navigation (it can land on the pre-navigation document and be wiped). These
// let the tabs.onUpdated listener below re-arm deterministically once the navigated tab reports
// complete, so a deck's last-track end never falls through to YTM autoplay.
let liveBoundaryVid = null, standbyBoundaryVid = null;
// Waiting-state net: focus the radio window at most once per blocked-autoplay episode (see the
// pevent relay below). Deliberately NOT persisted/restored with the rest of deck state: it is a
// transient UI nicety (avoid refocusing the window over and over for the same still-unresolved
// episode), not correctness-bearing, so an SW restart just defaulting it back to false is harmless.
let deckWaitingFocused = false;

// Mirror the tracked ids into session storage so a service-worker restart mid-session can recover
// them: module state is wiped on restart, but chrome.storage.session survives for the browser session.
// lastDeckEpoch rides along (C1, final review): it is deck-manager state like the ids, and losing it
// to an SW restart would make the next local boundary ack deck-toggled with epoch:null, which the
// backend's strict echo guard drops.
function saveDeckState() {
  try {
    chrome.storage.session.set({ tcDeckState: {
      deckWindowId, liveTabId, standbyTabId, deckGen, lastDeckEpoch,
      liveBoundaryVid, standbyBoundaryVid,
    } });
  } catch (e) {}
}
async function restoreDeckState() {
  try {
    const got = await chrome.storage.session.get("tcDeckState");
    const s = got && got.tcDeckState;
    if (!s) return;
    deckWindowId = s.deckWindowId != null ? s.deckWindowId : null;
    liveTabId = s.liveTabId != null ? s.liveTabId : null;
    standbyTabId = s.standbyTabId != null ? s.standbyTabId : null;
    deckGen = s.deckGen != null ? s.deckGen : null;
    lastDeckEpoch = s.lastDeckEpoch != null ? s.lastDeckEpoch : null;
    liveBoundaryVid = s.liveBoundaryVid != null ? s.liveBoundaryVid : null;
    standbyBoundaryVid = s.standbyBoundaryVid != null ? s.standbyBoundaryVid : null;
  } catch (e) {}
}
// SW-wake race: the service worker can be woken by a deck-* frame before restoreDeckState()'s async
// storage read has resolved, which would let a handler read the (still module-default) null ids and
// stomp on state a previous SW instance had already saved. Memoize the one restore promise and have
// every deck-* control handler await it before touching ids, so a wake mid-restore blocks briefly
// instead of racing.
let restoreDeckStatePromise = null;
function ensureDeckStateRestored() {
  if (!restoreDeckStatePromise) restoreDeckStatePromise = restoreDeckState();
  return restoreDeckStatePromise;
}
if (RADIO_ENABLED) ensureDeckStateRestored();

function tabForRole(role) {
  return role === "standby" ? standbyTabId : liveTabId;
}

// Same inject-retry pattern used throughout this file: send once, and on failure inject the content
// script then retry once.
// Returns whether the message was actually delivered (used by toggleDecks() to tell a genuine
// delivery from a swallowed failure before it acks a toggle to the backend).
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

function sendDeckReady(fallback, reason) {
  // Fallback diagnostics (visibility wave): every degradation call site now passes a short reason
  // string (the caught error's message, or a fixed string for the onRemoved paths) so the backend
  // can log + surface WHY dual fell back instead of the owner just seeing single-tab mode with no
  // explanation. Omitted to null on a non-fallback (successful) deck-ready: there is nothing to
  // explain there.
  try {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "deck-ready", gen: deckGen, fallback: !!fallback,
        reason: fallback ? (reason || null) : null,
      }));
    }
  } catch (e) {}
}

// Close the radio window IF the manager created it (deckWindowId is only ever set here), then
// normalize whatever survives (a partial failure can leave a tab behind even when the window itself
// failed to close), and clear all tracked deck state either way.
async function teardownDeckWindow() {
  if (deckWindowId != null) {
    try {
      await chrome.windows.remove(deckWindowId);
    } catch (e) {
      if (liveTabId != null) { try { await chrome.tabs.update(liveTabId, { muted: false }); } catch (e2) {} }
      if (standbyTabId != null) { try { await chrome.tabs.remove(standbyTabId); } catch (e2) {} }
    }
  }
  deckWindowId = null; liveTabId = null; standbyTabId = null; deckGen = null; lastDeckEpoch = null;
  liveBoundaryVid = null; standbyBoundaryVid = null;
  saveDeckState();
}

// Wait for a tab's content script to answer, bounded (mirrors createAndWaitForYtmTab's own loop
// further down this file). Declared here; pingContentScript/waitForTabComplete are defined below but
// hoisted (top-level function declarations), so the forward reference is safe.
async function waitTabUsable(tabId, budgetMs) {
  const deadline = Date.now() + budgetMs;
  await inject(tabId);
  while (Date.now() < deadline) {
    if (await pingContentScript(tabId)) return true;
    await inject(tabId);
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

// deck-start {liveUrl, standbyUrl, boundaryVideoId, gen}: stand up the dedicated radio window with the
// live deck as its visible (unfocused-window) active tab -- the ONE probe-verified way playback can
// start -- then a muted, inactive standby tab loaded and parked (content.js pauses it on load).
// Replies deck-ready {gen, fallback:false} once both tabs answer; ANY failure along the way tears the
// window back down and replies deck-ready {gen, fallback:true} so the backend keeps single-deck mode.
async function handleDeckStart(frame) {
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  deckGen = frame.gen;
  // C1 (final review): seed the epoch echo from deck-start itself. In a skip-free session no
  // deck-boundary-config/deck-toggle ever arrives before the first natural boundary, so without this
  // seed the first deck-toggled would ack epoch:null and be dropped by the backend's strict guard.
  lastDeckEpoch = frame.epoch != null ? frame.epoch : null;
  // C2 (final review): remember BOTH decks' boundary vids so the onUpdated listener below can re-arm
  // them deterministically after any (re)load; the standby's boundary is armed here too, otherwise a
  // promoted deck reaches its last track with no toggle trigger and leaks into YTM autoplay.
  liveBoundaryVid = frame.boundaryVideoId || null;
  standbyBoundaryVid = frame.standbyBoundaryVideoId || null;
  try {
    const win = await chrome.windows.create({
      url: frame.liveUrl, focused: false, width: 480, height: 360, left: 40, top: 40,
    });
    if (!win || win.id == null) throw new Error("radio window create failed");
    deckWindowId = win.id;
    const liveTab = (win.tabs && win.tabs[0]) || (await chrome.tabs.query({ windowId: win.id }))[0];
    if (!liveTab) throw new Error("live deck tab missing from new window");
    liveTabId = liveTab.id;
    // Owner-reported gap fixed here: cold start used to create the live tab active+UNMUTED and just
    // hope YTM autoplayed, which is fragile (gesture/MEI-dependent) and silent when it fails. Mirror
    // the exact probe-verified order toggleDecks already uses for every LATER swap: mute the tab
    // first, wait for load + content-script injection, THEN call deck-play (play() while the tab is
    // still muted is the one mechanism that works without a user gesture), verify delivery, and only
    // unmute after that succeeds -- see the deck-play call below, after waitTabUsable.
    try { await chrome.tabs.update(liveTabId, { muted: true }); } catch (e) {}
    // F6: a timed-out load is a failed critical step, same discipline as the verified-delivery throw
    // further down -- it must degrade via the catch below (teardown + sendDeckReady(true)), never
    // hang handleDeckStart with no deck-ready either way.
    if (!(await waitForTabComplete(liveTabId))) throw new Error("live deck tab never finished loading");

    const standbyTab = await chrome.tabs.create({
      windowId: deckWindowId, url: frame.standbyUrl, active: false,
    });
    standbyTabId = standbyTab.id;
    try { await chrome.tabs.update(standbyTabId, { muted: true }); } catch (e) {}
    if (!(await waitForTabComplete(standbyTabId))) throw new Error("standby deck tab never finished loading");

    const [liveOk, standbyOk] = await Promise.all([
      waitTabUsable(liveTabId, 10000),
      waitTabUsable(standbyTabId, 10000),
    ]);
    if (!liveOk || !standbyOk) throw new Error("deck content script never answered");

    if (frame.boundaryVideoId) await driveTab(liveTabId, "radio-boundary", { videoId: frame.boundaryVideoId });
    if (frame.standbyBoundaryVideoId) {
      await driveTab(standbyTabId, "radio-boundary", { videoId: frame.standbyBoundaryVideoId });
    }
    await driveTab(standbyTabId, "deck-pause-mute");   // belt and suspenders; the onUpdated listener below also arms this

    // The critical step, same discipline as toggleDecks' verified commit: a failed delivery must
    // never be treated as a working cold start, so it throws into the catch below (teardown +
    // sendDeckReady(true)) instead of unmuting a tab that never actually started playing. Only once
    // deck-play is confirmed delivered do we unmute at the tab level -- content.js itself reports a
    // blocked play() (rejected even while muted) as a "deck-waiting" pevent, handled independently of
    // this delivery check (see content.js's deck-play handler and the pevent relay below).
    if (!(await driveTab(liveTabId, "deck-play"))) throw new Error("deck-play not delivered to live tab");
    await chrome.tabs.update(liveTabId, { muted: false });

    saveDeckState();
    sendDeckReady(false);
  } catch (e) {
    console.warn("[TuneConsole] deck-start failed, falling back to single-deck:", e);
    await teardownDeckWindow();
    sendDeckReady(true, e && e.message);
  }
}

// deck-navigate {role, url}: point that deck's tab at a fresh URL; if the tab is gone (closed by the
// user), self-heal by recreating it in the radio window rather than giving up on the deck.
async function handleDeckNavigate(frame) {
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  const role = frame.role;
  const muted = role === "standby";
  const tabId = tabForRole(role);
  let exists = false;
  if (tabId != null) {
    try { await chrome.tabs.get(tabId); exists = true; } catch (e) { exists = false; }
  }
  if (!exists) {
    if (deckWindowId == null) return;   // no radio window at all: nothing to self-heal into
    // A dead tab can self-heal by recreating it in the radio window, but a dead WINDOW cannot: detect
    // that up front (rather than letting tabs.create fail silently below) and degrade instead.
    try {
      await chrome.windows.get(deckWindowId);
    } catch (e) {
      console.warn("[TuneConsole] deck-navigate: radio window is gone, degrading to single-deck fallback:", e);
      sendDeckReady(true, e && e.message);
      await teardownDeckWindow();
      return;
    }
    try {
      // Live bug (owner's SW console, 2026-07-05): chrome.tabs.create does NOT accept `muted` in its
      // createProperties -- that is tabs.update-only -- so passing it here threw
      // `TypeError: Unexpected property: 'muted'` on EVERY self-heal, which made any deck tab loss
      // permanently degrade the session to single-tab fallback (the exact silent failure this wave
      // instruments). Fixed the same way handleDeckStart already creates its standby tab: create
      // unmuted/inactive-as-appropriate, then mute via a separate tabs.update call.
      const t = await chrome.tabs.create({ windowId: deckWindowId, url: frame.url, active: !muted });
      if (muted) { try { await chrome.tabs.update(t.id, { muted: true }); } catch (e2) {} }
      if (role === "standby") standbyTabId = t.id; else liveTabId = t.id;
      saveDeckState();
    } catch (e) {
      // tabs.create can still fail here (e.g. the window died between the check above and now):
      // same degradation as a confirmed-dead window, not a silent swallow.
      console.warn("[TuneConsole] deck-navigate: self-heal tab create failed, degrading to single-deck fallback:", e);
      sendDeckReady(true, e && e.message);
      await teardownDeckWindow();
    }
    return;
  }
  try {
    await suppressBeforeUnload(tabId);
    await chrome.tabs.update(tabId, { url: frame.url, muted });
  } catch (e) {}
}

// deck-boundary-config {role, videoId, epoch}: arm the given deck's boundary vid. Track the epoch so
// the eventual deck-toggled echo carries it back verbatim (T7g contract amendment): never recomputed.
async function handleDeckBoundaryConfig(frame) {
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  lastDeckEpoch = frame.epoch;
  // C2 (final review): remember the deck's target boundary vid so the onUpdated listener can re-arm
  // it once a raced deck-navigate reload completes (this direct send can land on the pre-navigation
  // document and be wiped with it).
  if (frame.role === "standby") standbyBoundaryVid = frame.videoId || null;
  else liveBoundaryVid = frame.videoId || null;
  saveDeckState();
  const tabId = tabForRole(frame.role);
  if (tabId == null) return;
  await driveTab(tabId, "radio-boundary", { videoId: frame.videoId });
}

// The swap itself: activate the standby tab in-window (probe-verified visibility), play it WHILE
// STILL MUTED (the verified order), then unmute at the tab level; pause+mute the old live tab; swap
// the tracked ids; tell the backend it happened, echoing the epoch back verbatim.
//
// Both decks being briefly audible at once (the standby is unmuted before the old live tab is
// paused+muted) is BY DESIGN here: crossfade-like, and the probe-verified ordering constrains it.
// Do not "fix" by reordering.
async function toggleDecks() {
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  if (standbyTabId == null || liveTabId == null) return;
  const standby = standbyTabId, live = liveTabId;
  try {
    // Verify before committing: a stale id from a tab/window closed out from under us must degrade
    // gracefully rather than let us swap ids and ack a toggle that never actually happened (the
    // false-positive ack this whole try/catch exists to prevent).
    await chrome.tabs.get(standby);
    await chrome.tabs.get(live);

    // Each of these three is load-bearing for the toggle actually having happened; let a failure in
    // any of them fall through to the catch below instead of silently continuing.
    await chrome.tabs.update(standby, { active: true });
    if (!(await driveTab(standby, "deck-play"))) throw new Error("deck-play not delivered to standby tab");
    await chrome.tabs.update(standby, { muted: false });

    // Past this point the new live tab is already audible and committed; pausing/muting the OLD live
    // tab is best-effort (a failure here degrades that one tab, not the toggle's correctness).
    await driveTab(live, "deck-pause");
    try { await chrome.tabs.update(live, { muted: true }); } catch (e) {}

    liveTabId = standby; standbyTabId = live;
    // Keep the remembered boundary vids tab-correct across the swap (C2): the promoted tab's armed
    // vid is now the LIVE boundary; the demoted tab's stale vid gets overwritten by the standby
    // rebuild's deck-boundary-config.
    const swapBoundary = liveBoundaryVid;
    liveBoundaryVid = standbyBoundaryVid; standbyBoundaryVid = swapBoundary;
    saveDeckState();
    try {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "deck-toggled", epoch: lastDeckEpoch }));
      }
    } catch (e) {}
  } catch (e) {
    console.warn("[TuneConsole] toggleDecks failed, degrading to single-deck fallback:", e);
    // Do NOT swap ids, do NOT send deck-toggled: an ack here would be a false positive, telling the
    // backend playback flipped when it did not. Instead degrade gracefully via the existing
    // gen-guarded deck-ready handler (already flips the backend to single-deck fallback, no new
    // frame type needed) -- sent BEFORE teardown so it still carries the current deckGen. Then clear
    // local state; the radio window may already be dead, so its close is best-effort (teardownDeckWindow).
    sendDeckReady(true, e && e.message);
    await teardownDeckWindow();
  }
}

// deck-stop {}: tear down the radio window entirely (if we created it) and clear all deck bookkeeping.
async function handleDeckStop() {
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  await teardownDeckWindow();
}

// Keep the standby tab parked: whenever it (re)loads -- initial deck-start, a deck-navigate rebuild,
// or a self-heal recreate -- tell content.js to pause+mute immediately, so it never emits phantom
// audio before its eventual toggle-in. Also the deterministic boundary re-arm (C2, final review): a
// (re)load wiped the content script's boundaryVid, and the radio-boundary sent alongside the
// deck-navigate can have landed on the pre-navigation document, so re-send the remembered vid once
// the navigated document reports complete. The live tab gets the same re-arm (it only navigates in
// rare reconcile paths, but a wiped live boundary is exactly the C2 autoplay leak).
chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (!RADIO_ENABLED || info.status !== "complete") return;
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  if (tabId === standbyTabId) {
    driveTab(standbyTabId, "deck-pause-mute");
    if (standbyBoundaryVid) driveTab(standbyTabId, "radio-boundary", { videoId: standbyBoundaryVid });
  } else if (tabId === liveTabId && liveBoundaryVid) {
    driveTab(liveTabId, "radio-boundary", { videoId: liveBoundaryVid });
  }
});

// The radio window can die outside of anything we called (the user closes it by hand). Detect that
// and degrade exactly like a failed toggle/navigate does: no tab to close (the window took both with
// it), just clear state and tell the backend to fall back via the same gen-guarded deck-ready path.
// Awaits the restore first (L7, final review): an SW woken by this very close would otherwise read
// the module-default null ids and skip the fallback signal entirely.
chrome.windows.onRemoved.addListener(async (windowId) => {
  if (!RADIO_ENABLED) return;
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  if (deckWindowId == null || windowId !== deckWindowId) return;
  sendDeckReady(true, "radio window closed");
  deckWindowId = null; liveTabId = null; standbyTabId = null; deckGen = null; lastDeckEpoch = null;
  liveBoundaryVid = null; standbyBoundaryVid = null;
  saveDeckState();
});

// A single deck tab closing (its window still alive) is deliberately NOT handled here: the next
// deck-navigate frame self-heals it (see handleDeckNavigate). If the whole window went with it, the
// onRemoved listener above already covers that case. Kept minimal/no-op on purpose so the two
// listeners cannot race each other into a double-clear or a redundant self-heal. Still awaits the
// restore before reading ids (L7): a wake on this event must not log against null state.
chrome.tabs.onRemoved.addListener(async (tabId) => {
  // #101: a closed tab is neither playing nor rescuable. Bookkeeping only, and done up front,
  // because the deck logic below deliberately returns early for non-deck tabs (which is most of
  // them) and playingTabs would otherwise grow for the life of the service worker.
  playingTabs.delete(tabId);
  mediaMutedTabs.delete(tabId);
  pendingPlays.delete(tabId);
  if (playbackTabId === tabId) playbackTabId = null;
  if (rescue && rescue.tabId === tabId) cancelPlaybackRescue();
  if (!RADIO_ENABLED) return;
  await ensureDeckStateRestored();   // SW-wake race guard: see restoreDeckState's comment
  if (tabId !== liveTabId && tabId !== standbyTabId) return;
  console.log("[TuneConsole] deck tab closed", tabId, "- leaving self-heal to the next deck-navigate");
});

// F6: bounded -- a live/standby tab that never reaches "complete" (dead network, hung SPA boot) must
// not hang the caller forever with no deck-ready in either direction. Resolves true on completion,
// false on timeout (or a chrome.tabs.get error, e.g. the tab already gone), so callers can treat a
// timeout as a failed critical step exactly like any other probe failure in this file.
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
    console.log("[TuneConsole] no YTM tab for fetch, opening one in the background");
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
    console.log("[TuneConsole bridge] connected");
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
      try { result = await rateInYtmTab(frame.action, frame.videoId); }
      catch (e) { result = {ok: false, error: String(e)}; }
      try { sock.send(JSON.stringify({id: frame.id, status: 200, body: JSON.stringify(result)})); } catch (e) {}
      return;
    }
    if (frame && frame.type === "playpause") { playPauseInYtmTab(); return; }
    if (frame && frame.type === "refresh-view") { refreshViewInYtmTab(frame.playlist); return; }
    if (!RADIO_ENABLED && frame && RADIO_CONTROL_TYPES.has(frame.type)) return;
    if (frame && frame.type === "radio-prime") { primeRadioInYtmTab(frame.url); return; }
    // #93 v3 dual-deck (T7j): deck manager control frames from the backend.
    if (frame && frame.type === "deck-start") { handleDeckStart(frame); return; }
    if (frame && frame.type === "deck-navigate") { handleDeckNavigate(frame); return; }
    if (frame && frame.type === "deck-boundary-config") { handleDeckBoundaryConfig(frame); return; }
    if (frame && frame.type === "deck-toggle") { lastDeckEpoch = frame.epoch; toggleDecks(); return; }
    if (frame && frame.type === "deck-stop") { handleDeckStop(); return; }
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

// Push a now-playing event, and #91 raw pevent frames (both sent by the content script), up to the
// backend: "play" for the now-playing pipeline, "pevent" for the playback observer stream. Also the
// landing spot for the content script's local "deck-boundary" trigger (#93 v3): the live deck's
// tracked `ended` fires this straight from content.js, so the toggle happens immediately rather than
// waiting on a WS round trip; the epoch it echoes back was already recorded by
// handleDeckBoundaryConfig/deck-toggle, so toggleDecks() here needs no argument.
async function relayPlaybackMessage(msg, sender) {
  if (msg && msg.type === "deck-boundary") {
    if (!RADIO_ENABLED) return;
    // L6 (final review): only the LIVE deck tab's boundary may toggle. A stray `ended` in the old,
    // still-armed standby tab (e.g. the user manually plays it before its rebuild navigate lands)
    // must not fire a spurious swap. Async so an SW woken by this very message restores the tracked
    // ids before the gate reads them (toggleDecks re-awaits internally; that is idempotent).
    (async () => {
      await ensureDeckStateRestored();
      if (sender && sender.tab && sender.tab.id === liveTabId) toggleDecks();
      else console.log("[TuneConsole] ignoring deck-boundary from non-live tab",
        sender && sender.tab && sender.tab.id);
    })();
    return;
  }
  if (!msg) return;
  if (!RADIO_ENABLED && msg.type === "pevent" && msg.kind === "deck-waiting") return;
  // Frame tagging (#93 v3): every play/pevent relayed from a tracked deck tab carries which deck it
  // came from, so the backend can ignore frames from a muted, parked standby tab.
  //
  // ATTRIBUTION SUBTLETY for "deck-waiting" pevents (waiting-state net): during toggleDecks, deck-play
  // is sent to the about-to-be-promoted tab BEFORE liveTabId/standbyTabId swap. content.js's own
  // play() rejection (and its report back here) is async, so depending on timing it can land either
  // before or after that swap -- the SAME waiting episode can therefore arrive tagged "standby" OR
  // "live". Tag and forward it exactly like any other pevent (no special-casing here); bridge.py is
  // the one that must accept kind == "deck-waiting" from EITHER tag (see its pevent branch comment).
  const deck = sender && sender.tab && sender.tab.id === liveTabId ? "live"
    : sender && sender.tab && sender.tab.id === standbyTabId ? "standby" : "unknown";
  // Recycled-standby autoplay guard: a standby tab (possibly recycled from a previous live deck) must
  // never progress on its own. If a "play" report arrives FROM the tracked standby tab, immediately
  // pause it again -- self-correcting, and independent of whatever happens to the frame below, which
  // is still tagged+forwarded as "standby" so the backend's existing drop-standby-frames logic covers it.
  if (msg.type === "play" && deck === "standby") {
    driveTab(standbyTabId, "deck-pause");
  }
  // Waiting-state net: a blocked deck-play (content.js's own rejection report) means the deck window
  // opened with nothing audible. Focus it once per waiting episode so the owner actually sees it needs
  // a click, best-effort (a failure here is not worth degrading anything over). The once-guard resets
  // the moment a real play frame arrives from either deck tab, so the NEXT waiting episode still
  // focuses the window instead of staying silent forever after the first one.
  if (msg.type === "pevent" && msg.kind === "deck-waiting" && deck !== "unknown") {
    if (deckWindowId != null && !deckWaitingFocused) {
      deckWaitingFocused = true;
      // F3 (nit): .catch it too, not just try/catch -- chrome.windows.update returns a promise, and a
      // rejection (window closed between the null check and the call) would otherwise be an
      // unhandled-rejection console warning rather than the best-effort no-op this is meant to be.
      try { chrome.windows.update(deckWindowId, { focused: true }).catch(() => {}); } catch (e) {}
    }
  }
  if (msg.type === "play" && (deck === "live" || deck === "standby")) {
    deckWaitingFocused = false;
  }
  // #101 Playback rescue: track which tabs are actually PLAYING (content.js reports paused off the
  // real <video>, so a paused play frame is not playback), and stand the rescue down the moment the
  // navigated tab reports real sound. That is the "if needed" in the issue: the switch only ever
  // happens when the background swap genuinely failed to start.
  const srcTabId = sender && sender.tab && sender.tab.id;
  if ((msg.type === "play" || msg.type === "now-heartbeat") && srcTabId != null && deck !== "standby") {
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
  if (msg.type === "pevent" && ["state", "bye"].includes(msg.kind) && deck !== "standby") {
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
      tabMuted: !!msg.tabMuted, timeZone: msg.timeZone, deck }));
    pendingPlays.delete(srcTabId);
  } else if (msg.type === "now-heartbeat") {
    // Metadata makes the non-persisting heartbeat able to restore a card that aged out while Chrome
    // throttled the YTM page (commonly during an audio-device switch).
    ws.send(JSON.stringify({ type: "now-heartbeat", deck, title: msg.title, artist: msg.artist,
      metadataVerified: msg.metadataVerified !== false,
      thumbnail: msg.thumbnail, likeStatus: msg.likeStatus, videoId: msg.videoId,
      playlist: msg.playlist || "", albumBrowseId: msg.albumBrowseId || "", paused: !!msg.paused,
      muted: !!msg.muted, tabMuted: !!msg.tabMuted }));
  } else if (msg.type === "pevent") {
    ws.send(JSON.stringify(Object.assign({}, msg, { deck })));   // #91 already a flat, self-describing frame
  }
}

// Tab queries are asynchronous; serialize reports so an older snapshot cannot overtake a newer
// play/pause or account change while ownership is being resolved.
let playbackRelay = Promise.resolve();
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || !["play", "now-heartbeat", "pevent", "deck-boundary"].includes(msg.type)) return;
  playbackRelay = playbackRelay.then(() => relayPlaybackMessage(msg, sender))
    .catch(error => console.warn("[TuneConsole] playback relay failed", error));
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
