// Content script: runs inside the music.youtube.com page (isolated world). Two jobs:
//  1. Perform authenticated youtubei fetches for the service worker, so the traffic shows up in the
//     page's own DevTools Network tab (not hidden in the service worker).
//  2. Watch what is playing and report track changes to the backend (via the service worker).
// Guarded so being injected more than once (declared plus on-demand) does not double-register.

if (!window.__tcBridgeLoaded) {
  window.__tcBridgeLoaded = true;
  const SENSOR_VERSION = 4;
  let mainFrameAt = null;
  let sensorProbeSerial = 0;
  const sensorProbes = new Map();

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

  let pendingPlaylistRefresh = null;
  const reconcilePlaylistRefresh = () => {
    if (!pendingPlaylistRefresh) return;
    try {
      if (!playlistMatches(pendingPlaylistRefresh)) {
        // A different SPA view must not be reloaded by an edit to the previous playlist.
        pendingPlaylistRefresh = null;
        return;
      }
      const video = document.querySelector("video");
      if (video && !video.paused && !video.ended) return;
      location.reload();
      pendingPlaylistRefresh = null;
    } catch (e) {} // Keep the request if a transient failure prevented the reload.
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
  const clickRate = (action, videoId, snapshot, desiredStatus) => {
    const bar = domNowPlaying();
    // A native button click only needs a fresh, matching player/control identity.
    // Missing recording credits or account metadata must not disable the heart.
    // History ingestion keeps its own stricter metadata/account checks.
    if (!["like", "dislike"].includes(action) || !videoId || !snapshot ||
        snapshot.videoId !== videoId || !snapshot.playerBar ||
        snapshot.playerBar.videoId !== videoId || !snapshot.playerBar.likeStatus ||
        (bar?.videoId && bar.videoId !== videoId)) {
      const error = `Rating identity mismatch: requested=${videoId || ''}, player=${snapshot?.videoId || ''}, ` +
        `control=${snapshot?.playerBar?.videoId || ''}, dom=${bar?.videoId || ''}, probe=${!!snapshot}`;
      return {ok: false, error};
    }
    const r = likeRenderer();
    if (!r) return {ok: false, error: "Player rating control is unavailable"};
    const current = readLikeStatus() || snapshot.playerBar.likeStatus;
    if (desiredStatus === current) return {ok: true};
    const buttonAction = desiredStatus === 'LIKE' ? 'like' : desiredStatus === 'DISLIKE' ? 'dislike' :
      desiredStatus === 'INDIFFERENT' ? (current === 'LIKE' ? 'like' : 'dislike') : action;
    const id = buttonAction === "like" ? "#button-shape-like" : "#button-shape-dislike";
    const btn = r.querySelector(id + " button") || r.querySelector(id);
    if (!btn) return {ok: false, error: "Player rating button is unavailable"};
    btn.click();
    return {ok: true};
  };

  // Drive YouTube Music's own play/pause control in the player bar, same shape as clickRate.
  const clickPlayPause = () => {
    const btn = document.querySelector("ytmusic-player-bar #play-pause-button");
    if (!btn) return;
    const inner = btn.querySelector("button") || btn;
    inner.click();
  };

  // Start the requested queue on browse pages, or resume the player on watch pages. A global
  // ytmusic-play-button-renderer lookup can select an unrelated song card in a shelf.
  const pagePlayButton = () => document.querySelector(
    "ytmusic-detail-header-renderer ytmusic-play-button-renderer, " +
    "ytmusic-responsive-header-renderer ytmusic-play-button-renderer, " +
    "ytmusic-immersive-header-renderer ytmusic-play-button-renderer"
  );
  const clickPlaybackStart = () => {
    const v = document.querySelector("video");
    if (v && !v.paused) return; // a pending play() must never be toggled back to paused
    const main = new URL(location.href).pathname.startsWith("/browse/") && pagePlayButton();
    if (main) {
      (main.querySelector("button") || main).click();
    } else {
      clickPlayPause();
    }
  };
  const playbackStarted = () => {
    const v = document.querySelector("video");
    return !!(v && !v.paused && !v.ended && v.readyState >= 2);
  };
  const playbackDelay = () => new Promise(resolve => setTimeout(resolve, 250));
  const ensurePlaying = async () => {
    if (playbackStarted()) return { playing: true };
    const v = document.querySelector("video");
    const browse = new URL(location.href).pathname.startsWith("/browse/");
    if (browse) {
      // Do not resume the previous song instead of starting the album/playlist just opened.
      clickPlaybackStart();
    } else if (v && v.paused) {
      try {
        // Do not await play(): it can remain pending indefinitely while media is loading.
        const pending = v.play();
        if (pending) pending.catch(() => {});
      } catch (e) {}
    }
    await playbackDelay();
    if (playbackStarted()) return { playing: true };
    if (!browse) clickPlaybackStart();
    await playbackDelay();
    return { playing: playbackStarted() };
  };

  const sensorStatus = () => {
    const v = document.querySelector("video");
    return { ok: true, hasVideo: !!v, paused: !!(v && v.paused), url: location.href,
      mainVersion: mainFrameAt === null ? null : SENSOR_VERSION,
      mainAgeMs: mainFrameAt === null ? null : Math.max(0, Date.now() - mainFrameAt) };
  };
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "sensor-ping") {
      // Fetch readiness needs only this receiver. Health also asks MAIN to sample immediately,
      // so timer throttling in background tabs does not look like a failed sensor.
      if (!msg.main) { sendResponse(sensorStatus()); return; }
      const id = ++sensorProbeSerial;
      const finish = () => {
        if (!sensorProbes.has(id)) return;
        clearTimeout(timer);
        sensorProbes.delete(id);
        sendResponse(sensorStatus());
      };
      const timer = setTimeout(finish, 750);
      sensorProbes.set(id, finish);
      try { window.postMessage({ __tcSensorProbe: { version: SENSOR_VERSION, id } }, "*"); }
      catch (e) { finish(); }
      return true;
    }
    if (msg.type === "rate") {
      const id = ++sensorProbeSerial;
      const timer = setTimeout(() => finish(null), 1250);
      const finish = (snapshot) => {
        if (!sensorProbes.has(id)) return;
        sensorProbes.delete(id);
        clearTimeout(timer);
        try { sendResponse(clickRate(msg.action, msg.videoId, snapshot, msg.likeStatus)); }
        catch (e) { sendResponse({ok: false, error: String(e)}); }
      };
      sensorProbes.set(id, finish);
      try { window.postMessage({__tcSensorProbe: {version: SENSOR_VERSION, id}}, "*"); }
      catch (e) { finish(null); }
      return true;
    }
    if (msg.type === "playpause") { clickPlayPause(); return; }
    if (msg.type === "ensure-playing") {
      ensurePlaying().then(sendResponse, () => sendResponse({ playing: false }));
      return true; // keep the response channel open until playback has been checked
    }
    if (msg.type === "pause") {
      // Pause only a playing tab, so this never resumes an already-paused player.
      try {
        const v = document.querySelector("video");
        if (v && !v.paused) clickPlayPause();
      } catch (e) {}
      return;
    }
    if (msg.type === "resync-now") {
      // Reconnect needs a fresh player identity and verification result. The DOM can restore
      // presence while MAIN answers, but cannot turn stale bar bindings into a persisted play.
      lastNowPlaying = "";
      const np = domNowPlaying();
      if (np) report(np);
      try { window.postMessage({ __tcSensorProbe: { version: SENSOR_VERSION,
        id: ++sensorProbeSerial } }, "*"); } catch (e) {}
      return;
    }
    if (msg.type === "refresh-view") {
      // Coalesce edits to this playlist until a reload can reconcile the view/queue without
      // interrupting music. The periodic sensor retries the original request after playback stops.
      if (!playlistMatches(msg.playlist)) return;
      pendingPlaylistRefresh = msg.playlist;
      reconcilePlaylistRefresh();
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
        sendResponse({ status: resp.status, body: await resp.text(),
          timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone });
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
  let lastAccount = null; // null means unknown; an empty brandId means the verified main account.
  const report = (np) => {
    if (!np || !np.title) return;
    const vid = np.videoId || "";
    const lst = np.playlist || "";
    // MAIN captures these fields together with the bar's identity. DOM fallback must prove its
    // identity too; a bar still showing the preceding song cannot rate the new playing video.
    const bar = np.playerBar || domNowPlaying();
    const matches = !!vid && bar && bar.videoId === vid;
    const likeStatus = matches ? bar.likeStatus || "" : "";
    const albumBrowseId = matches ? bar.albumBrowseId || "" : "";
    // The MediaSession and DOM can format the same title/artist differently. Use the video id
    // when available so switching sources does not re-report a play. Account/rating changes still do.
    const key = JSON.stringify([np.brandId, vid || [np.title, np.artist], likeStatus]);
    // The backend ages this card out when the YTM tab disappears. Keep its presence fresh when
    // paused or unchanged, without recording another play. Chrome can throttle both sensors in a
    // background tab, causing them to take turns reporting the same paused track.
    if (np.paused || !vid || !np.brandKnown || np.metadataVerified !== true || key === lastNowPlaying) {
      // Carry the current snapshot as well as presence. Chrome can suspend/throttle this page while
      // audio devices change; if that gap exceeds the backend's stale-card timeout, a presence-only
      // heartbeat cannot rebuild Now Playing and the UI stays blank until the next track. This frame
      // is still a heartbeat (never a persisted play), but is now self-healing.
      try { chrome.runtime.sendMessage({
        type: "now-heartbeat", title: np.title, artist: np.artist, thumbnail: np.thumbnail,
        metadataVerified: np.metadataVerified === true,
        likeStatus, videoId: vid, playlist: lst, albumBrowseId, paused: !!np.paused,
        muted: !!np.muted, brandKnown: !!np.brandKnown, brandId: np.brandId || "",
      }); } catch (e) {}
      return;
    }
    lastNowPlaying = key;
    chrome.runtime.sendMessage({
      type: "play", title: np.title, artist: np.artist, thumbnail: np.thumbnail,
      metadataVerified: np.metadataVerified === true,
      likeStatus, videoId: vid, playlist: lst, albumBrowseId, brandId: np.brandId || "",
      paused: !!np.paused, muted: !!np.muted, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
  };
  let lastMainAt = null;
  let lastMain = null;
  let lastPlayer = null;   // #91 latest playback point, for the pagehide goodbye
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data) return;
    // Store updates can leave a legacy MAIN-world interval in the page. Its URL-derived identity
    // must not compete with the new player-derived sensor injected without interrupting music.
    if ((ev.data.__tcPlayer || ev.data.__tcNow) && ev.data.__tcSensorVersion !== SENSOR_VERSION) return;
    if (ev.data.__tcPlayer) {
      const p = ev.data.__tcPlayer;
      lastAccount = p.brandKnown ? { brandId: p.brandId || "" } : null;
      if (!p.brandKnown) return; // Unknown account must not become main-account playback history.
      lastPlayer = { videoId: p.videoId, position: p.position, duration: p.duration, brandId: p.brandId };
      try { chrome.runtime.sendMessage(Object.assign({ type: "pevent" }, p)); } catch (e) {}
      return;
    }
    if (ev.data.__tcCuration) {
      try { chrome.runtime.sendMessage(Object.assign({ type: "pevent" }, ev.data.__tcCuration)); } catch (e) {}
      return;
    }
    if (!ev.data.__tcNow) return;
    mainFrameAt = Date.now(); // Incomplete snapshots still prove that MAIN is alive.
    const finishProbe = sensorProbes.get(ev.data.__tcSensorProbeId);
    const np = ev.data.__tcNow;
    lastAccount = np.brandKnown ? { brandId: np.brandId || "" } : null;
    if (np.videoId && np.title) {
      lastMainAt = Date.now();
      lastMain = np;
      report(np);
    } else {
      // A live but incomplete MAIN sensor must not starve a usable player-bar fallback.
      lastMainAt = null;
      const fallback = domNowPlaying();
      if (fallback && np.playerBar) fallback.playerBar = np.playerBar;
      report(fallback);
    }
    if (finishProbe) finishProbe(np);
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
      const v = document.querySelector("video");
      let videoId = "", playlist = "";
      // Only the player bar describes playback. A browsed watch/playlist URL is unrelated.
      const link = document.querySelector('ytmusic-player-bar a[href*="watch?"]');
      try {
        if (link) {
          const u = new URL(link.href, location.href);
          videoId = u.searchParams.get("v") || "";
          if (videoId) playlist = u.searchParams.get("list") || "";
        }
        if (img) {
          const u = new URL(img.src);
          const match = /\/vi(?:_webp)?\/([^/]+)/.exec(u.pathname);
          if (u.hostname.endsWith(".ytimg.com") && match) {
            if (videoId && videoId !== match[1]) { videoId = ""; playlist = ""; }
            else videoId = match[1];
          }
        }
      } catch (e) {}
      let albumBrowseId = "";
      const albumLink = document.querySelector(
        'ytmusic-player-bar a[href*="/browse/MPRE"], ' +
        'ytmusic-player-bar a[href*="/browse/FEmusic_library_privately_owned_release_detail"]');
      if (videoId && albumLink) {
        try { albumBrowseId = decodeURIComponent(new URL(albumLink.href, location.href).pathname.split("/browse/")[1] || ""); } catch (e) {}
      }
      // Keep the actual queue through MAIN-world throttling only when the DOM confirms its song.
      if (videoId && lastMain && lastMain.videoId === videoId && lastAccount &&
          lastMain.brandKnown && lastMain.brandId === lastAccount.brandId) playlist = lastMain.playlist || "";
      return {
        title: t.textContent.trim(),
        // Matching DOM fields describe the bar, not proof that its bindings describe the player.
        // Only a fresh MAIN sample can verify metadata for a persisted play.
        metadataVerified: false,
        artist: b ? b.textContent.trim().split("•")[0].trim() : "",
        thumbnail: img ? img.src : "",
        videoId, playlist, likeStatus: videoId ? readLikeStatus() : "", albumBrowseId,
        brandKnown: lastAccount !== null,
        brandId: lastAccount ? lastAccount.brandId : "",
        paused: !v || v.paused,
        muted: !!v && (v.muted || v.volume === 0),
      };
    }
    return null;
  };
  setInterval(() => {
    // Deferred playlist edits must progress even while the primary now-playing sensor is healthy.
    reconcilePlaylistRefresh();
    // A complete MAIN snapshot is primary. Fall back when it is unavailable, incomplete, or stale.
    if (lastMainAt !== null && Date.now() - lastMainAt < 5000) return;
    const np = domNowPlaying();
    if (np) report(np);
  }, 2000);
}
