// Runs in the PAGE's main world (declared with "world": "MAIN"), so it can read YouTube Music's real
// navigator.mediaSession.metadata, which the isolated content script cannot see. It posts the current
// track to the isolated content script (via window.postMessage), which forwards it to the backend.
(function () {
  if (window.__tcNowPoller) return;
  window.__tcNowPoller = true;
  setInterval(function () {
    try {
      var md = navigator.mediaSession && navigator.mediaSession.metadata;
      if (!md || !md.title) return;
      var art = md.artwork && md.artwork.length ? md.artwork[md.artwork.length - 1].src : "";
      window.postMessage({ __tcNow: { title: md.title, artist: md.artist || "", thumbnail: art || "" } }, "*");
    } catch (e) {}
  }, 2000);
})();
