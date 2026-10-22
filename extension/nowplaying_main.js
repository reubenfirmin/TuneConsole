// Runs in the PAGE's main world (declared with "world": "MAIN"), so it can read YouTube Music's
// real navigator.mediaSession and <video> element, which the isolated content script cannot see.
// Posts the current track (__tcNow) plus raw player events (__tcPlayer, #91) to the
// isolated content script via window.postMessage. Dumb sensor: no judgments here, the server
// classifies skips/completions/sessions from the raw stream.
(function () {
  var SENSOR_VERSION = 4;
  var SENSOR_REVISION = 1; // Implementation changes can replace a compatible, healthy sensor.
  var previous = window.__tcNowPoller;
  if (previous && previous.version === SENSOR_VERSION && previous.revision === SENSOR_REVISION && !previous.stopped &&
      previous.lastSampleAt != null && Date.now() - previous.lastSampleAt < 10000) return;
  if (previous && previous.stop) previous.stop();

  var TICK_MS = 30000;   // position heartbeat while playing (crash insurance)
  var last = { videoId: "", playlist: "", position: 0, duration: 0 };
  var lastTickAt = 0;
  var bindings = [];

  function playerInfo() {
    // The URL describes the page being browsed, which can differ from the playing song/queue.
    // Read both identifiers from the same player instead; unknown context stays empty.
    var info = { videoId: "", playlist: "", title: "", artist: "", thumbnail: "" };
    try {
      var host = document.querySelector("ytmusic-player");
      var player = document.querySelector("#movie_player");
      if (!player || typeof player.getVideoData !== "function") player = host && host.playerApi;
      var data = player && player.getVideoData && player.getVideoData();
      if (data) {
        info.videoId = data.video_id || "";
        info.title = data.title || "";
        info.artist = data.author || "";
      }
      if (info.videoId && player.getPlaylistId) info.playlist = player.getPlaylistId() || "";
      // Album-art URLs often have no video ID. The player response supplies that identity
      // separately, so it can cover a stale/missing bar without trusting stale MediaSession art.
      if (info.videoId && typeof player.getPlayerResponse === "function") {
        var response = player.getPlayerResponse();
        var details = response && response.videoDetails;
        var thumbs = details && details.thumbnail && details.thumbnail.thumbnails;
        if (details && details.videoId === info.videoId && Array.isArray(thumbs)) {
          for (var i = thumbs.length - 1; i >= 0; i--) {
            if (thumbs[i] && typeof thumbs[i].url === "string" && thumbs[i].url) {
              info.thumbnail = thumbs[i].url;
              break;
            }
          }
        }
      }
    } catch (e) {}
    return info;
  }
  function account() {
    try {
      var cfg = window.ytcfg;
      if (cfg && cfg.get) {
        var delegated = cfg.get("DELEGATED_SESSION_ID");
        // An absent config is not proof of the main account. A populated signed-in config with
        // no delegated session is; an explicit delegated session also identifies the account.
        if ((typeof delegated === "string" && delegated) || cfg.get("LOGGED_IN") === true) {
          return { brandKnown: true, brandId: delegated || "" };
        }
      }
    } catch (e) {}
    return { brandKnown: false, brandId: "" };
  }
  function playerBarInfo() {
    var bar = { videoId: "", title: "", artist: "", thumbnail: "", likeStatus: "", albumBrowseId: "" };
    try {
      var host = document.querySelector("ytmusic-player-bar");
      var title = document.querySelector(".title.ytmusic-player-bar") || document.querySelector("ytmusic-player-bar .title");
      var byline = document.querySelector(".byline.ytmusic-player-bar") || document.querySelector("ytmusic-player-bar .byline");
      var img = document.querySelector("img.image.ytmusic-player-bar") || document.querySelector("ytmusic-player-bar img");
      var link = document.querySelector('ytmusic-player-bar a[href*="watch?"]');
      // MAIN can read Polymer's track data even when album art has no video ID and the bar has
      // no watch link. Reject conflicting identifiers while its bindings catch up to a change.
      var ids = [];
      if (host && host.data && typeof host.data.videoId === "string" && host.data.videoId) ids.push(host.data.videoId);
      if (link) {
        var id = new URL(link.href, location.href).searchParams.get("v");
        if (id) ids.push(id);
      }
      bar.thumbnail = (img && img.src) || "";
      if (bar.thumbnail) {
        var u = new URL(bar.thumbnail);
        var match = /\/vi(?:_webp)?\/([^/]+)/.exec(u.pathname);
        if (u.hostname.endsWith(".ytimg.com") && match) ids.push(match[1]);
      }
      if (ids.length && ids.every(function (id) { return id === ids[0]; })) bar.videoId = ids[0];
      bar.title = title ? title.textContent.trim() : "";
      bar.artist = byline ? byline.textContent.trim().split("•")[0].trim() : "";
      if (!bar.videoId) return bar;
      var rating = document.querySelector("ytmusic-player-bar ytmusic-like-button-renderer");
      var target = rating && rating.data && rating.data.target;
      if (rating && (!target || !target.videoId || target.videoId === bar.videoId)) {
        var like = rating.querySelector("#button-shape-like button");
        var dislike = rating.querySelector("#button-shape-dislike button");
        bar.likeStatus = rating.getAttribute("like-status") ||
          ((like && like.getAttribute("aria-pressed") === "true") ? "LIKE" :
           (dislike && dislike.getAttribute("aria-pressed") === "true") ? "DISLIKE" : "INDIFFERENT");
      }
      var album = document.querySelector('ytmusic-player-bar a[href*="/browse/MPRE"], ' +
        'ytmusic-player-bar a[href*="/browse/FEmusic_library_privately_owned_release_detail"]');
      if (album) bar.albumBrowseId = decodeURIComponent(new URL(album.href, location.href).pathname.split("/browse/")[1] || "");
    } catch (e) {}
    return bar;
  }
  function metadata(info, bar) {
    var md = navigator.mediaSession && navigator.mediaSession.metadata;
    var art = "";
    // A matching bar preserves album artwork and recording credits without relying on the image
    // URL to contain a video ID. MediaSession alone still needs an explicit matching video ID.
    if (info.videoId && md && md.artwork) {
      for (var i = md.artwork.length - 1; i >= 0; i--) {
        try {
          var u = new URL(md.artwork[i].src);
          var match = /\/vi(?:_webp)?\/([^/]+)/.exec(u.pathname);
          if (u.hostname.endsWith(".ytimg.com") && match && match[1] === info.videoId) {
            art = md.artwork[i].src;
            break;
          }
        } catch (e) {}
      }
    }
    if (info.videoId && bar.videoId === info.videoId && bar.title && bar.artist) {
      return { title: bar.title, artist: bar.artist,
               thumbnail: bar.thumbnail || art || info.thumbnail, verified: true };
    }
    return { title: (art && md.title) || info.title,
             artist: (art && md.artist) || info.artist, thumbnail: art || info.thumbnail,
             verified: !!(art && md.title && md.artist) };
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
      var identity = account();
      var base = { kind: kind, videoId: last.videoId, position: last.position,
                   duration: last.duration, playlist: last.playlist,
                   shuffle: sr.shuffle, repeat: sr.repeat,
                   brandId: identity.brandId, brandKnown: identity.brandKnown };
      if (extra) { for (var k in extra) base[k] = extra[k]; }
      window.postMessage({ __tcSensorVersion: SENSOR_VERSION, __tcPlayer: base }, "*");
    } catch (e) {}
  }
  function observe(v) {
    var info = playerInfo();
    if (!info.videoId) return info;
    if (last.videoId && info.videoId !== last.videoId) {
      // Preserve the old queue as well as its last position when reporting the exit.
      post("track_exit", {});
    }
    if (info.videoId !== last.videoId) lastTickAt = 0;
    last = { videoId: info.videoId, playlist: info.playlist,
             position: (v && v.currentTime) || 0, duration: (v && v.duration) || 0 };
    return info;
  }
  function bindVideo(v) {
    if (!v || bindings.some(function (b) { return b.video === v; })) return;
    function listen(event, kind, extra) {
      var handler = function () {
        if (v !== document.querySelector("video")) return;
        // YTM's own ended listener can already have advanced its API to the next song. Finish
        // the observed track before a later play event/poll adopts that new identity.
        if (event === "ended" && last.videoId) {
          last.position = v.currentTime || last.position;
          last.duration = v.duration || last.duration;
          post(kind, extra());
          return;
        }
        // Media events can happen between polls; sample their actual identity and position.
        if (observe(v).videoId) post(kind, extra());
      };
      v.addEventListener(event, handler);
      bindings.push({ video: v, event: event, handler: handler });
    }
    listen("ended", "ended", function () { return {}; });
    listen("pause", "state", function () { return { state: "paused" }; });
    listen("play", "state", function () { return { state: "playing" }; });
    listen("volumechange", "volume", function () { return { volume: v.muted ? 0 : v.volume }; });
  }

  function sample(probeId) {
    try {
      var v = document.querySelector("video");
      bindVideo(v);
      var info = observe(v);
      var now = Date.now();
      if (info.videoId && v && !v.paused && now - lastTickAt >= TICK_MS) {
        lastTickAt = now;
        post("tick", {});
      }
      var bar = playerBarInfo();
      var meta = metadata(info, bar), identity = account();
      // Send account availability even before the player is ready, so DOM fallback can safely
      // attribute a play without borrowing identity from a previous track.
      window.postMessage({ __tcSensorVersion: SENSOR_VERSION, __tcSensorProbeId: probeId,
                           __tcNow: { title: meta.title, artist: meta.artist, thumbnail: meta.thumbnail,
                                      metadataVerified: meta.verified,
                                      videoId: info.videoId, playlist: info.playlist,
                                      playerBar: bar,
                                      brandId: identity.brandId, brandKnown: identity.brandKnown,
                                      paused: !v || v.paused,
                                      muted: !!v && (v.muted || v.volume === 0) } }, "*");
      poller.lastSampleAt = Date.now();
    } catch (e) {}
  }
  function onProbe(ev) {
    var probe = ev.source === window && ev.data && ev.data.__tcSensorProbe;
    if (probe && probe.version === SENSOR_VERSION) sample(probe.id);
  }
  var timer = setInterval(sample, 2000);
  window.addEventListener("message", onProbe);
  // Future updates can replace this sensor without reloading a tab or leaving duplicate listeners.
  // A legacy boolean marker cannot stop its old interval; content.js ignores its unversioned frames.
  var poller = window.__tcNowPoller = { version: SENSOR_VERSION, revision: SENSOR_REVISION,
    lastSampleAt: null, stopped: false, stop: function () {
    poller.stopped = true;
    clearInterval(timer);
    window.removeEventListener("message", onProbe);
    bindings.forEach(function (b) { b.video.removeEventListener(b.event, b.handler); });
  } };
  sample(); // Reinjection can prove MAIN health immediately, before its first periodic tick.
})();
