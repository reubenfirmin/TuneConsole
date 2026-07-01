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
  } catch (e) {}
}
async function injectAllYtmTabs() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  for (const t of tabs) await inject(t.id);
}

// On extension install/reload, reload any open YouTube Music tabs so they pick up the fresh content
// script. That way reloading the extension is the only reload you need, no manual tab refresh.
async function reloadYtmTabs() {
  const tabs = await chrome.tabs.query({ url: "https://music.youtube.com/*" });
  for (const t of tabs) {
    try { await chrome.tabs.reload(t.id); } catch (e) {}
  }
}

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

async function handleFrame(frame) {
  // Allowlist is the load-bearing control: refuse anything not a youtubei call.
  if (!isAllowed(frame.url)) {
    return { id: frame.id, status: 0, body: JSON.stringify({ error: "blocked by allowlist" }) };
  }
  const tab = await firstYtmTab();
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
  sock.onopen = () => {
    console.log("[TuneConsole bridge] connected");
    injectAllYtmTabs();   // make sure existing tabs have the content script (and now-playing watcher)
  };
  sock.onmessage = async (ev) => {
    const frame = JSON.parse(ev.data);
    if (frame && frame.ping) return; // keepalive from the backend; receiving it keeps this SW awake
    if (frame && frame.type === "navigate") { navigateYtmTab(frame.url); return; }
    if (frame && frame.type === "rate") { rateInYtmTab(frame.action); return; }
    const reply = await handleFrame(frame);
    try { sock.send(JSON.stringify(reply)); } catch (e) {}
  };
  sock.onclose = () => { if (ws === sock) { ws = null; setTimeout(connect, 3000); } };
  sock.onerror = () => { try { sock.close(); } catch (e) {} };
}

// Push a now-playing event (sent by the content script) up to the backend so it can log the play.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "play" && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "play", title: msg.title, artist: msg.artist,
      thumbnail: msg.thumbnail, likeStatus: msg.likeStatus }));
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
