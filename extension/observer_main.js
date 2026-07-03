// Runs in the PAGE's main world at document_start, so it wraps window.fetch BEFORE YouTube Music's
// own scripts capture a reference. #91: a read-only observer for a small allowlist of curation
// endpoints (likes, playlist edits, library feedback, subscriptions, share intent). It never
// alters, blocks, or reorders requests, and it fails open: any error means a lost observation,
// never a broken page. Observed at REQUEST time, so success is not guaranteed (raw signal).
(function () {
  if (window.__tcFetchHook) return;
  window.__tcFetchHook = true;

  var PATTERNS = [
    { re: /\/youtubei\/v1\/like\/(like|removelike|dislike)([?/]|$)/, kind: "rate" },
    { re: /\/youtubei\/v1\/browse\/edit_playlist([?/]|$)/, kind: "playlist_edit" },
    { re: /\/youtubei\/v1\/feedback([?/]|$)/, kind: "feedback" },
    { re: /\/youtubei\/v1\/subscription\/(subscribe|unsubscribe)([?/]|$)/, kind: "subscription" },
    { re: /\/youtubei\/v1\/share\/get_share_panel([?/]|$)/, kind: "share_intent" }
  ];
  var BODY_CAP = 4096;

  function brand() {
    try { return (window.ytcfg && window.ytcfg.get) ? (window.ytcfg.get("DELEGATED_SESSION_ID") || "") : ""; } catch (e) { return ""; }
  }

  var orig = window.fetch;
  window.fetch = function (input, init) {
    try {
      var url = typeof input === "string" ? input : ((input && input.url) || "");
      for (var i = 0; i < PATTERNS.length; i++) {
        if (PATTERNS[i].re.test(url)) {
          var body = (init && typeof init.body === "string") ? init.body : "";
          window.postMessage({ __tcCuration: {
            kind: PATTERNS[i].kind, url: url.split("?")[0], body: body.slice(0, BODY_CAP),
            href: location.href, brandId: brand()
          } }, "*");
          break;
        }
      }
    } catch (e) {}
    return orig.apply(this, arguments);
  };
})();
