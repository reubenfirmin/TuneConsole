// Service worker: holds the persistent WebSocket to the local backend, enforces the endpoint
// allowlist, and relays each allowed request to the content script for an in-page fetch (so
// traffic is visible in the standard music.youtube.com DevTools Network tab). No YouTube fetch
// happens here; this file only decides whether a request is allowed and where to find a tab
// that can make it.

const ORIGIN = "https://music.youtube.com";
let ws = null;

function isAllowed(u) {
  try {
    const x = new URL(u);
    return x.origin === "https://music.youtube.com" && x.pathname.startsWith("/youtubei/v1/");
  } catch (e) {
    return false;
  }
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

async function firstYtmTab() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  return tabs[0] || null;
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
  } catch (e) {}
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

// On extension install/reload, reload any open YouTube Music tabs so they pick up the fresh content
// script. That way reloading the extension is the only reload you need, no manual tab refresh.
async function reloadYtmTabs() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  for (const t of tabs) {
    try { await chrome.tabs.reload(t.id); } catch (e) {}
  }
}

// Note: rate/playpause/radio-prime below intentionally stay no-ops when there is no YTM tab (via
// pickYtmTab returning null). A rate/playpause/radio-prime control is meaningless without a tab
// already playing something, so unlike the fetch path there is nothing useful to open a tab for.

// Prefer the active/most-recently-used YouTube Music tab (the one the user is actually listening in).
async function pickYtmTab() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  if (!tabs.length) return null;
  const active = tabs.find((t) => t.active);
  if (active) return active;
  return tabs.slice().sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0))[0];
}

// Land a backend "navigate" request in the EXISTING YouTube Music tab, not a new one. Scoped to
// music.youtube.com so the backend can only ever point our own tab at YouTube Music.
async function navigateYtmTab(url) {
  try {
    if (new URL(url).origin !== ORIGIN) return;
  } catch (e) {
    return;
  }
  const tab = await pickYtmTab();
  if (tab) {
    // Reuse the existing tab, this is the common case (you already have YouTube Music open). Do NOT
    // activate it: the swap happens in the background so you stay on TuneConsole.
    try {
      await suppressBeforeUnload(tab.id);   // seamless swap, no "Reload site?" prompt
      await chrome.tabs.update(tab.id, { url });
      console.log("[TuneConsole] navigated existing YTM tab (background)", tab.id, "->", url);
    } catch (e) {
      console.warn("[TuneConsole] navigate update failed:", e);
    }
  } else {
    // Only when there is genuinely no YouTube Music tab: open one (in the background), since there is
    // nothing to swap.
    console.log("[TuneConsole] no existing YTM tab, opening one in the background");
    try { await chrome.tabs.create({ url, active: false }); } catch (e) {}
  }
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
async function rateInYtmTab(action) {
  const tab = await pickYtmTab();
  if (!tab) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "rate", action });
  } catch (e) {
    await inject(tab.id);
    try { await chrome.tabs.sendMessage(tab.id, { type: "rate", action }); } catch (e2) {}
  }
}

// Ask the content script to play/pause the current track by driving YTM's own player-bar control.
async function playPauseInYtmTab() {
  const tab = await pickYtmTab();
  if (!tab) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "playpause" });
  } catch (e) {
    await inject(tab.id);
    try { await chrome.tabs.sendMessage(tab.id, { type: "playpause" }); } catch (e2) {}
  }
}

// Ask the content script to refresh the YTM tab's view so edits made in TuneConsole (rename, art,
// tracklist, etc.) show up promptly without ever interrupting playback. The content script owns the
// "is it safe to reload" decision (it checks the URL and playback state); this just delivers the ask.
async function refreshViewInYtmTab(playlist) {
  const tab = await pickYtmTab();
  if (!tab) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "refresh-view", playlist });
  } catch (e) {
    await inject(tab.id);
    try { await chrome.tabs.sendMessage(tab.id, { type: "refresh-view", playlist }); } catch (e2) {}
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
ensureDeckStateRestored();

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
  if (info.status !== "complete") return;
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
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) { finish(false); return; }
      if (tab && tab.status === "complete") finish(true);
    });
    timer = setTimeout(() => finish(false), budgetMs);
  });
}

// Non-destructive probe for "is the content script listening yet": resync-now is already used this
// same way on bridge reconnect (see resyncNowPlaying) and has no side effect worth avoiding here, so
// reuse it instead of inventing a new message type in content.js.
async function pingContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "resync-now" });
    return true;
  } catch (e) {
    return false;
  }
}

// Open a YTM tab in the background and wait for it to be usable: loaded, content script injected,
// and answering messages. Same inject-and-retry pattern as rate/playpause/etc, just looped with a
// deadline instead of a single retry, since a freshly created tab can take a while to load.
async function createAndWaitForYtmTab() {
  console.log("[TuneConsole] no YTM tab for fetch, opening one in the background");
  let tab;
  try {
    tab = await chrome.tabs.create({ url: `${ORIGIN}/`, active: false });
  } catch (e) {
    return null;
  }
  const deadline = Date.now() + 15000;
  await Promise.race([waitForTabComplete(tab.id), new Promise((r) => setTimeout(r, 15000))]);
  await inject(tab.id);
  while (Date.now() < deadline) {
    if (await pingContentScript(tab.id)) return tab;
    await inject(tab.id);
    await new Promise((r) => setTimeout(r, 300));
  }
  return null; // timed out; caller falls back to the existing "open music.youtube.com" error
}

// Dedupe: several fetches can race in while there is no YTM tab open. They must not each open their
// own tab, so share a single in-flight "create and wait" promise; once it settles, forget it so a
// later miss (e.g. the tab got closed again) can open a fresh one.
let ensureTabPromise = null;
async function ensureYtmTab() {
  const existing = await firstYtmTab();
  if (existing) return existing;
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
    // The tab likely predates the extension load; inject the content script and retry once.
    await inject(tab.id);
    try {
      res = await sendFetch(tab.id, frame, auth);
      if (!res) throw new Error("no response from content script");
    } catch (e2) {
      return {
        id: frame.id, status: 0,
        body: JSON.stringify({ error: "content script not ready, reload the music.youtube.com tab" }),
      };
    }
  }
  return { id: frame.id, status: res.status, body: res.body };
}

// The backend authenticates us by our extension origin, so there is nothing to configure: we just
// connect to the local bridge on the default port. The backend binds 127.0.0.1 on this same port.
const BRIDGE_URL = "ws://127.0.0.1:8765/bridge/ws";

function connect() {
  if (ws && ws.readyState !== WebSocket.CLOSED) return;
  const sock = new WebSocket(BRIDGE_URL);
  ws = sock;
  sock.onopen = async () => {
    console.log("[TuneConsole bridge] connected");
    await injectAllYtmTabs();   // make sure existing tabs have the content script (and now-playing watcher)
    resyncNowPlaying();         // backend cleared now_playing on the last disconnect; re-emit the current track
  };
  sock.onmessage = async (ev) => {
    const frame = JSON.parse(ev.data);
    if (frame && frame.ping) return; // keepalive from the backend; receiving it keeps this SW awake
    if (frame && frame.type === "navigate") { navigateYtmTab(frame.url); return; }
    if (frame && frame.type === "rate") { rateInYtmTab(frame.action); return; }
    if (frame && frame.type === "playpause") { playPauseInYtmTab(); return; }
    if (frame && frame.type === "refresh-view") { refreshViewInYtmTab(frame.playlist); return; }
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
  sock.onclose = () => { if (ws === sock) { ws = null; setTimeout(connect, 3000); } };
  sock.onerror = () => { try { sock.close(); } catch (e) {} };
}

// Push a now-playing event, and #91 raw pevent frames (both sent by the content script), up to the
// backend: "play" for the now-playing pipeline, "pevent" for the playback observer stream. Also the
// landing spot for the content script's local "deck-boundary" trigger (#93 v3): the live deck's
// tracked `ended` fires this straight from content.js, so the toggle happens immediately rather than
// waiting on a WS round trip; the epoch it echoes back was already recorded by
// handleDeckBoundaryConfig/deck-toggle, so toggleDecks() here needs no argument.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "deck-boundary") {
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
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (msg.type === "play") {
    ws.send(JSON.stringify({ type: "play", title: msg.title, artist: msg.artist,
      thumbnail: msg.thumbnail, likeStatus: msg.likeStatus, videoId: msg.videoId,
      playlist: msg.playlist || "", brandId: msg.brandId || "", paused: !!msg.paused, deck }));
  } else if (msg.type === "pevent") {
    ws.send(JSON.stringify(Object.assign({}, msg, { deck })));   // #91 already a flat, self-describing frame
  }
});

// Belt and suspenders: an alarm wakes the service worker periodically so it reconnects if the
// socket ever dropped while it was suspended (the backend ping keeps it alive while connected).
chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onInstalled.addListener(reloadYtmTabs);   // reload open YTM tabs so one reload suffices
connect();

// Quality of life: keep a single TuneConsole tab. When a second one appears (the app relaunches and
// opens another, or you open one by hand), close the extras and focus the one that was already there
// so you never end up with a pile of 127.0.0.1:8765 tabs.
const APP_TAB_GLOB = "http://127.0.0.1:8765/*";
let dedupeBusy = false;
async function dedupeAppTabs() {
  if (dedupeBusy) return;
  dedupeBusy = true;
  try {
    const tabs = await chrome.tabs.query({ url: APP_TAB_GLOB });
    if (tabs.length <= 1) return;
    tabs.sort((a, b) => a.id - b.id);       // keep the oldest tab (preserves its state)
    const keep = tabs[0];
    for (const t of tabs.slice(1)) { try { await chrome.tabs.remove(t.id); } catch (e) {} }
    try {
      await chrome.tabs.update(keep.id, { active: true });
      if (keep.windowId != null) await chrome.windows.update(keep.windowId, { focused: true });
    } catch (e) {}
  } catch (e) {
    // no tabs permission / query failed: nothing to do
  } finally {
    dedupeBusy = false;
  }
}
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url && tab.url.startsWith("http://127.0.0.1:8765/")) {
    dedupeAppTabs();
  }
});
