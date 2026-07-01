// Runs in the page's MAIN world at document_start, BEFORE YouTube Music registers its own
// beforeunload handler. A capturing listener registered first runs first, and stopImmediatePropagation
// prevents YouTube Music's handler from running, so swapping the tab to a new playlist (when
// TuneConsole plays one) never triggers the "Leave site? Changes you made may not be saved" prompt.
window.addEventListener(
  "beforeunload",
  function (e) { e.stopImmediatePropagation(); },
  true,
);
