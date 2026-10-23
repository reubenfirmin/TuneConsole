// One status owner for the whole document. Components consume bridge-status events and can
// read the cached snapshot when they mount after the first response or an HTMX replacement.
(function () {
  if (window.playbackStatus) return;
  let timer = null, inFlight = null;

  function publish(data) {
    window.playbackStatus.snapshot = data;
    window.dispatchEvent(new CustomEvent('bridge-status', {detail: data}));
  }

  function refresh() {
    if (inFlight) return inFlight.promise;
    const request = {controller: new AbortController(), promise: null};
    inFlight = request;
    request.promise = fetch('/bridge/status', {signal: request.controller.signal})
      .then(response => {
        if (!response.ok) throw new Error('Playback status unavailable');
        return response.json();
      })
      .then(data => {
        if (!data || typeof data.connected !== 'boolean') throw new Error('Invalid playback status');
        if (inFlight === request) publish(data);
      })
      .catch(() => {
        if (inFlight !== request) return;
        // Retain Home's connection knowledge on a transient failure, but clear stale playback,
        // the radio indicator and sensor details until a fresh snapshot arrives.
        publish({connected: !!window.playbackStatus.snapshot?.connected});
      })
      .finally(() => { if (inFlight === request) inFlight = null; });
    return request.promise;
  }

  function start() {
    if (timer !== null) return;
    timer = setInterval(refresh, document.getElementById('home') ? 3000 : 2500);
    refresh();
  }

  function stop() {
    clearInterval(timer);
    timer = null;
    const request = inFlight;
    inFlight = null;
    request?.controller.abort();
  }

  window.playbackStatus = {snapshot: null, start, refresh, stop};
  window.addEventListener('pagehide', stop);
  window.addEventListener('pageshow', start);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
