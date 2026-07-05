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

  // Does this tab's current URL point at the given playlist? Either the watch page's querystring
  // (?list=<id>, browsing a track within the playlist) or the playlist's own browse page
  // (/browse/VL<id>) count as "looking at this playlist".
  const playlistMatches = (playlist) => {
    if (!playlist) return false;
    try {
      const u = new URL(location.href);
      if (u.searchParams.get("list") === playlist) return true;
      return u.pathname.includes("browse/VL" + playlist);
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

  // Drive YouTube Music's own play/pause control in the player bar, same shape as clickRate.
  const clickPlayPause = () => {
    const btn = document.querySelector("ytmusic-player-bar #play-pause-button");
    if (!btn) { console.warn("[TuneConsole] playpause: player-bar control not found"); return; }
    const inner = btn.querySelector("button") || btn;
    inner.click();
    console.log("[TuneConsole] toggled play/pause");
  };

  // Waiting-state net: report a blocked deck-play as a "deck-waiting" pevent (background relays it
  // and focuses the radio window) and arm a ONE-TIME user-activation retry: whichever of click/
  // keydown fires first satisfies Chrome's gesture requirement and retries play(). Shared by both
  // the "no <video> yet" case (F1: previously a silent no-op) and a play() rejection.
  //
  // waitingRetryHandler tracks the currently-armed pair so a later call (a new deck-play arrival, OR
  // the retry itself failing, F2) removes the previous un-fired pair before installing a new one:
  // un-fired pairs must not accumulate across episodes.
  let waitingRetryHandler = null;
  const clearWaitingRetry = () => {
    if (!waitingRetryHandler) return;
    document.removeEventListener("click", waitingRetryHandler);
    document.removeEventListener("keydown", waitingRetryHandler);
    waitingRetryHandler = null;
  };
  const armWaitingRetry = (errName) => {
    try {
      chrome.runtime.sendMessage({ type: "pevent", kind: "deck-waiting", detail: { err: errName } });
    } catch (e2) {}
    clearWaitingRetry();
    let fired = false;
    const retryPlay = () => {
      if (fired) return;
      fired = true;
      clearWaitingRetry();
      try {
        const v2 = document.querySelector("video");
        if (!v2) { armWaitingRetry("no-video"); return; }
        v2.play().catch((e3) => {
          // F2: the retry's own play() can fail too. Do not spend the episode silently -- re-report
          // deck-waiting and re-install a fresh listener pair so the NEXT click/keydown retries
          // again. Only re-arms once THIS pair has actually fired (clearWaitingRetry above already
          // removed it), so pairs cannot stack.
          armWaitingRetry(e3 && e3.name);
        });
      } catch (e3) {}
    };
    waitingRetryHandler = retryPlay;
    document.addEventListener("click", retryPlay, { once: true });
    document.addEventListener("keydown", retryPlay, { once: true });
  };

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "rate") { clickRate(msg.action); return; }
    if (msg.type === "playpause") { clickPlayPause(); return; }
    if (msg.type === "radio-prime") {
      // #93 a null/absent url means "clear the stored prime" (sent by /radio/stop), never a URL to
      // navigate to: without this, a stale prime from a stopped radio would hijack the tab on the
      // next organic track end.
      primedUrl = msg.url || null;
      return;
    }
    if (msg.type === "radio-boundary") {
      // #93 v3 dual-deck (T7j/T7k): background arms/disarms this deck's boundary vid. Ending it is
      // the toggle trigger (see the `ended` handoff below). A falsy videoId clears it (deck-stop).
      boundaryVid = msg.videoId || null;
      return;
    }
    if (msg.type === "deck-pause-mute" || msg.type === "deck-pause") {
      // #93 v3 dual-deck: keep a standby tab silent -- pause it the instant it (re)loads, and pause
      // the old live tab right after a toggle. Drive the player-bar control only if actually
      // playing, so this never flips an already-paused tab into playing.
      try {
        const v = document.querySelector("video");
        if (v && !v.paused) clickPlayPause();
      } catch (e) {}
      return;
    }
    if (msg.type === "deck-play") {
      // #93 v3 dual-deck: the newly-live tab must call play() while the TAB IS STILL MUTED (the
      // probe-verified order); background unmutes at the tab level right after this resolves.
      //
      // Owner-reported gap: play() can still be rejected even while muted (autoplay policy / MEI),
      // and the old silent catch swallowed that -- the deck window opened with nothing audible and no
      // signal anywhere that anything was wrong. F1: a missing <video> element (SPA player not yet
      // constructed despite the tab reporting complete) used to no-op the same way -- silent, no
      // play(), no report. Both paths now go through armWaitingRetry, which reports "deck-waiting"
      // (with a distinguishing err -- "no-video" for the missing-element case, e.name for a play()
      // rejection) and arms the one-time activation retry. Never throws either way.
      try {
        const v = document.querySelector("video");
        if (!v) {
          armWaitingRetry("no-video");
        } else if (v.paused) {
          v.play().catch((e) => armWaitingRetry(e && e.name));
        }
      } catch (e) {}
      return;
    }
    if (msg.type === "resync-now") {
      // The bridge (re)connected. The backend cleared now_playing on the previous disconnect, so
      // force a re-emit of the current track even though it hasn't changed (dedup would otherwise
      // swallow it until the next track). The MAIN-world poller re-emits within ~2s too.
      lastNowPlaying = "";
      const np = domNowPlaying();
      if (np) report(np);
      return;
    }
    if (msg.type === "refresh-view") {
      // Backend edits (rename, art, tracklist) should show up promptly, but the user's verification
      // glance must never cost them their playback. Only reload if this tab is actually looking at
      // the edited playlist, and only when nothing is audibly playing.
      try {
        if (!playlistMatches(msg.playlist)) return;
        const video = document.querySelector("video");
        // Absolute guard: never reload a tab with an in-progress playback. A stale view is fine; a
        // dropped song is not.
        if (video && !video.paused) return;
        location.reload();
      } catch (e) {}
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
  let primedUrl = null;  // #93 dynamic radio: the next track's watch URL, handed off on track end.
                          // A plain in-scope let: a hard navigation reloads the content script and
                          // this is wiped for free, so a stale prime cannot survive across pages.
  let boundaryVid = null;  // #93 v3 dual-deck: this deck's last track; ending it toggles decks.
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
      paused: !!np.paused,
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
      // #93 the instant the track ends, hand off to the primed radio track before YouTube Music
      // autoplays its own next. suppress_unload_main.js already neutralized the unload prompt.
      if (p.kind === "ended") {
        // #93 v3: at the live deck's boundary, toggle decks (background owns the tab swap: it
        // plays the standby tab while still muted, then unmutes, per the probe-verified order).
        // Otherwise fall back to the v2 re-sync: navigate ONLY to a playlist watch url (carries
        // list=), never a bare url that would hand off to YouTube Music's own radio.
        if (boundaryVid && p.videoId === boundaryVid) {
          // L9 (final review): one boundary, one toggle. Clear before firing so a re-delivered
          // `ended` for the same track cannot trigger a second deck-boundary; background re-arms
          // this deck's boundary explicitly when it should fire again.
          const bv = boundaryVid; boundaryVid = null;
          try { chrome.runtime.sendMessage({ type: "deck-boundary", videoId: bv }); } catch (e) {}
        } else if (primedUrl && primedUrl.includes("list=")) {
          const u = primedUrl; primedUrl = null;
          try { location.href = u; } catch (e) {}
        }
      }
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
  // #93 v3 dual-deck S1 (skip-at-boundary): the live deck's own "next" control would hand off to
  // YouTube Music's own radio past our last queued track. While parked at the boundary track,
  // intercept it (capture phase, ahead of YTM's own handler) and toggle decks ourselves instead.
  document.addEventListener("click", (ev) => {
    if (!boundaryVid) return;
    try {
      const next = ev.target.closest &&
        ev.target.closest("ytmusic-player-bar .next-button, tp-yt-paper-icon-button.next-button");
      const u = new URL(location.href);
      if (next && u.searchParams.get("v") === boundaryVid) {
        ev.preventDefault();
        ev.stopImmediatePropagation();
        // L9: same clear-before-fire as the `ended` handoff above, so a double click cannot send a
        // second deck-boundary for the same toggle.
        const bv = boundaryVid; boundaryVid = null;
        chrome.runtime.sendMessage({ type: "deck-boundary", videoId: bv });
      }
    } catch (e) {}
  }, true);
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
