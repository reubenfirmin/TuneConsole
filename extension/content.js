// Content script: runs inside the music.youtube.com page (isolated world). Two jobs:
//  1. Perform authenticated youtubei fetches for the service worker, so the traffic shows up in the
//     page's own DevTools Network tab (not hidden in the service worker).
//  2. Watch what is playing and report track changes to the backend (via the service worker).
// Guarded so being injected more than once (declared plus on-demand) does not double-register.

if (!window.__tcBridgeLoaded) {
  window.__tcBridgeLoaded = true;

  const isAllowed = (u) => {
    try {
      const x = new URL(u);
      return x.origin === "https://music.youtube.com" && x.pathname.startsWith("/youtubei/v1/");
    } catch (e) {
      return false;
    }
  };

  // Read/drive YouTube Music's own like/dislike control in the player bar, so rating the current
  // track goes through YTM exactly as a manual click would (no videoId juggling).
  const likeRenderer = () => document.querySelector("ytmusic-player-bar ytmusic-like-button-renderer");
  const readLikeStatus = () => {
    const r = likeRenderer();
    if (!r) return "";
    const s = r.getAttribute("like-status");
    if (s) return s; // LIKE | DISLIKE | INDIFFERENT
    const like = r.querySelector("#button-shape-like button");
    const dislike = r.querySelector("#button-shape-dislike button");
    if (like && like.getAttribute("aria-pressed") === "true") return "LIKE";
    if (dislike && dislike.getAttribute("aria-pressed") === "true") return "DISLIKE";
    return "INDIFFERENT";
  };
  const clickRate = (action) => {
    const r = likeRenderer();
    if (!r) { console.warn("[TuneConsole] rate: player-bar like control not found"); return; }
    const id = action === "like" ? "#button-shape-like" : "#button-shape-dislike";
    const btn = r.querySelector(id + " button") || r.querySelector(id);
    if (btn) { btn.click(); console.log("[TuneConsole] rated", action); }
    else console.warn("[TuneConsole] rate: button not found for", action);
  };

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "rate") { clickRate(msg.action); return; }
    if (msg.type === "resync-now") {
      // The bridge (re)connected. The backend cleared now_playing on the previous disconnect, so
      // force a re-emit of the current track even though it hasn't changed (dedup would otherwise
      // swallow it until the next track). The MAIN-world poller re-emits within ~2s too.
      lastNowPlaying = "";
      const np = domNowPlaying();
      if (np) report(np);
      return;
    }
    if (msg.type !== "fetch") return;
    (async () => {
      if (!isAllowed(msg.url)) {
        sendResponse({ status: 0, body: JSON.stringify({ error: "blocked by allowlist" }) });
        return;
      }
      try {
        const resp = await fetch(msg.url, {
          method: msg.method,
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            Authorization: msg.auth,
            "X-Origin": "https://music.youtube.com",
            "X-Goog-AuthUser": "0",
          },
          body: msg.method === "POST" ? JSON.stringify(msg.body || {}) : undefined,
        });
        sendResponse({ status: resp.status, body: await resp.text() });
      } catch (e) {
        sendResponse({ status: 0, body: JSON.stringify({ error: String(e) }) });
      }
    })();
    return true; // async sendResponse
  });

  // Now-playing: the isolated world cannot read the page's MediaSession, so the primary source is a
  // MAIN-world companion (nowplaying_main.js) that reads it and postMessages track changes here. We
  // also poll the player-bar DOM as a fallback. Either source feeds report(); dedup keeps it quiet.
  let lastNowPlaying = "";
  const report = (np) => {
    if (!np || !np.title) return;
    np.likeStatus = readLikeStatus();
    // Include likeStatus in the key so a like/dislike change re-reports even when the track is the same.
    const key = np.title + " | " + np.artist + " | " + np.likeStatus + " | " + (np.videoId || "");
    if (key === lastNowPlaying) return;
    lastNowPlaying = key;
    let vid = np.videoId || "", lst = np.playlist || "";
    if (!vid || !lst) {  // DOM-fallback reports carry neither; the watch URL has both
      try {
        const u = new URL(location.href);
        vid = vid || u.searchParams.get("v") || "";
        lst = lst || u.searchParams.get("list") || "";
      } catch (e) {}
    }
    console.log("[TuneConsole] now playing:", np.title, "-", np.artist, np.likeStatus);
    chrome.runtime.sendMessage({
      type: "play", title: np.title, artist: np.artist, thumbnail: np.thumbnail,
      likeStatus: np.likeStatus, videoId: vid, playlist: lst, brandId: np.brandId || "",
    });
  };
  let lastMainAt = 0;
  let lastPlayer = null;   // #91 latest playback point, for the pagehide goodbye
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data) return;
    if (ev.data.__tcPlayer) {
      const p = ev.data.__tcPlayer;
      lastPlayer = { videoId: p.videoId, position: p.position, duration: p.duration, brandId: p.brandId };
      try { chrome.runtime.sendMessage(Object.assign({ type: "pevent" }, p)); } catch (e) {}
      return;
    }
    if (ev.data.__tcCuration) {
      try { chrome.runtime.sendMessage(Object.assign({ type: "pevent" }, ev.data.__tcCuration)); } catch (e) {}
      return;
    }
    if (!ev.data.__tcNow) return;
    lastMainAt = Date.now();
    report(ev.data.__tcNow);
  });
  window.addEventListener("pagehide", () => {
    // #91 goodbye: close the session and the last track's record when the tab goes away.
    try {
      if (lastPlayer) chrome.runtime.sendMessage(Object.assign({ type: "pevent", kind: "bye" }, lastPlayer));
    } catch (e) {}
  });
  const domNowPlaying = () => {
    const t = document.querySelector(".title.ytmusic-player-bar") ||
              document.querySelector("ytmusic-player-bar .title");
    const b = document.querySelector(".byline.ytmusic-player-bar") ||
              document.querySelector("ytmusic-player-bar .byline");
    const img = document.querySelector("img.image.ytmusic-player-bar") ||
                document.querySelector("ytmusic-player-bar img");
    if (t && t.textContent.trim()) {
      return {
        title: t.textContent.trim(),
        artist: b ? b.textContent.trim().split("•")[0].trim() : "",
        thumbnail: img ? img.src : "",
      };
    }
    return null;
  };
  setInterval(() => {
    // The MAIN-world MediaSession is the primary source. Only fall back to the DOM when it has gone
    // quiet, so the two never both report (their titles differ slightly, which was double-logging).
    if (Date.now() - lastMainAt < 5000) return;
    const np = domNowPlaying();
    if (np) report(np);
  }, 2000);
}
