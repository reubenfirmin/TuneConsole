// Runs in the PAGE's main world (declared with "world": "MAIN"), so it can read YouTube Music's
// real navigator.mediaSession and <video> element, which the isolated content script cannot see.
// Posts the current track (__tcNow, unchanged) plus raw player events (__tcPlayer, #91) to the
// isolated content script via window.postMessage. Dumb sensor: no judgments here, the server
// classifies skips/completions/sessions from the raw stream.
(function () {
  if (window.__tcNowPoller) return;
  window.__tcNowPoller = true;

  var TICK_MS = 30000;   // position heartbeat while playing (crash insurance)
  var last = { videoId: "", position: 0, duration: 0 };
  var lastTickAt = 0;

  function urlInfo() {
    var vid = "", lst = "";
    try {
      var u = new URL(location.href);
      vid = u.searchParams.get("v") || "";
      lst = u.searchParams.get("list") || "";
    } catch (e) {}
    return { videoId: vid, playlist: lst };
  }
  function brand() {
    try { return (window.ytcfg && window.ytcfg.get) ? (window.ytcfg.get("DELEGATED_SESSION_ID") || "") : ""; } catch (e) { return ""; }
  }
  function shuffleRepeat() {
    // Best-effort context; unknown stays "". Repeat mode is an attribute on the player bar. The
    // shuffle button carries NO state attribute (verified against the live DOM), so shuffle comes
    // from the app store instead: probe the queue state for a boolean shuffle-ish key rather than
    // pinning an exact internal name YTM could rename.
    var rep = "", shuf = "";
    try {
      var bar = document.querySelector("ytmusic-player-bar");
      rep = (bar && (bar.getAttribute("repeat-mode_") || bar.getAttribute("repeat-mode"))) || "";
    } catch (e) {}
    try {
      var app = document.querySelector("ytmusic-app");
      var st = app && app.store && app.store.getState && app.store.getState();
      var q = st && st.queue;
      if (q) {
        for (var k in q) {
          if (typeof q[k] === "boolean" && /shuffle/i.test(k)) { shuf = q[k] ? "true" : "false"; break; }
        }
      }
    } catch (e) {}
    return { repeat: rep, shuffle: shuf };
  }
  function post(kind, extra) {
    try {
      var sr = shuffleRepeat();
      var base = { kind: kind, videoId: last.videoId, position: last.position,
                   duration: last.duration, playlist: urlInfo().playlist,
                   shuffle: sr.shuffle, repeat: sr.repeat, brandId: brand() };
      if (extra) { for (var k in extra) base[k] = extra[k]; }
      window.postMessage({ __tcPlayer: base }, "*");
    } catch (e) {}
  }
  function bindVideo(v) {
    if (!v || v.__tcBound) return;
    v.__tcBound = true;
    v.addEventListener("ended", function () { post("ended", {}); });
    v.addEventListener("pause", function () { post("state", { state: "paused" }); });
    v.addEventListener("play", function () { post("state", { state: "playing" }); });
    v.addEventListener("volumechange", function () { post("volume", { volume: v.muted ? 0 : v.volume }); });
  }

  setInterval(function () {
    try {
      var v = document.querySelector("video");
      bindVideo(v);
      var info = urlInfo();
      if (v && info.videoId === last.videoId) {   // refresh the observed point for the CURRENT track
        last.position = v.currentTime || 0;
        last.duration = v.duration || 0;
      }
      if (info.videoId && info.videoId !== last.videoId) {
        // Track changed: report where the OLD track was left, then adopt the new one.
        var prev = last.videoId ? { videoId: last.videoId, position: last.position, duration: last.duration } : null;
        last = { videoId: info.videoId, position: (v && v.currentTime) || 0, duration: (v && v.duration) || 0 };
        if (prev) post("track_exit", prev);
      }
      var now = Date.now();
      if (v && !v.paused && now - lastTickAt >= TICK_MS) {
        lastTickAt = now;
        post("tick", {});
      }
      // The original now-playing report, unchanged shape (the play pipeline consumes it).
      var md = navigator.mediaSession && navigator.mediaSession.metadata;
      if (md && md.title) {
        var art = md.artwork && md.artwork.length ? md.artwork[md.artwork.length - 1].src : "";
        // #97 carry the video's real paused state so a play frame taken right after a server
        // restart (or any resync) reflects reality instead of assuming "playing".
        window.postMessage({ __tcNow: { title: md.title, artist: md.artist || "", thumbnail: art || "",
                                        videoId: info.videoId, playlist: info.playlist, brandId: brand(),
                                        paused: !!(v && v.paused) } }, "*");
      }
    } catch (e) {}
  }, 2000);
})();
