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
      var vid = "", lst = "";
      try {
        var u = new URL(location.href);
        vid = u.searchParams.get("v") || "";
        lst = u.searchParams.get("list") || "";   // #75 playlist provenance for the play event
      } catch (e) {}
      var brand = "";
      try {  // brand-account tabs carry the delegated (brand) id; the main account has none
        brand = (window.ytcfg && window.ytcfg.get) ? (window.ytcfg.get("DELEGATED_SESSION_ID") || "") : "";
      } catch (e) {}
      window.postMessage({ __tcNow: { title: md.title, artist: md.artist || "", thumbnail: art || "",
                                      videoId: vid, playlist: lst, brandId: brand } }, "*");
    } catch (e) {}
  }, 2000);
})();
