// Runs in the PAGE's main world at document_start, so it wraps window.fetch BEFORE YouTube Music's
// own scripts capture a reference. #91: a read-only observer for a small allowlist of curation
// endpoints (likes, playlist edits, library feedback, subscriptions, share intent). It never
// alters, blocks, or reorders requests, and it fails open: any error means a lost observation,
// never a broken page. Ratings include the fetch outcome; other events are raw request intent.
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
    var observation = null;
    var bodyReady = Promise.resolve("");
    try {
      var url = typeof input === "string" ? input : ((input && input.url) || "");
      for (var i = 0; i < PATTERNS.length; i++) {
        if (PATTERNS[i].re.test(url)) {
          observation = {kind: PATTERNS[i].kind, url: url.split("?")[0],
                         href: location.href, brandId: brand()};
          // YTM usually passes the JSON body inside a Request object, not as a string init.body;
          // clone() lets us read it without consuming the stream. Async is fine: the server stamps
          // event time at receipt, and a failed read still reports the observation (empty body).
          if (init && typeof init.body === "string") bodyReady = Promise.resolve(init.body);
          else if (input && typeof input.clone === "function") {
            // Clone before fetch can consume the original Request stream.
            try { bodyReady = input.clone().text().catch(function () { return ""; }); }
            catch (e) {}
          }
          break;
        }
      }
    } catch (e) {}
    function report(outcome) {
      if (!observation) return;
      bodyReady.then(function (body) {
        try {
          window.postMessage({__tcCuration: Object.assign({}, observation, outcome,
            {body: (body || "").slice(0, BODY_CAP)})}, "*");
        } catch (e) {}
      }).catch(function () {});
    }
    if (observation && observation.kind !== "rate") report({phase: "intent"});
    var result;
    try { result = orig.apply(this, arguments); }
    catch (error) {
      if (observation && observation.kind === "rate")
        report({phase: "settled", success: false, httpStatus: 0, error: String(error)});
      throw error;
    }
    if (observation && observation.kind === "rate") {
      // Observe a separate branch, returning the exact promise/response/rejection to the page.
      Promise.resolve(result).then(function (response) {
        report({phase: "settled", success: response.ok === true, httpStatus: response.status});
      }, function (error) {
        report({phase: "settled", success: false, httpStatus: 0, error: String(error)});
      }).catch(function () {});
    }
    return result;
  };
})();
