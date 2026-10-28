// One status owner for the whole document. Components consume bridge-status events and can
// read the cached snapshot when they mount after the first response or an HTMX replacement.
(function () {
  if (window.playbackStatus) return;
  let timer = null, inFlight = null, frame = null, expiry = null, sample = null, lastFrame = 0, bridgeSnapshot = null;
  const clock = () => window.performance?.now() ?? Date.now();
  const number = value => typeof value === 'number' && Number.isFinite(value);

  function progress() {
    if (!sample) return null;
    const age = sample.age + Math.max(0, clock() - sample.receivedAt) / 1000;
    // Match the server's heartbeat expiry even if the next HTTP request hangs.
    if (age >= 8) return null;
    let position = sample.position;
    if (position !== null && !sample.paused) position += age;
    if (position !== null && sample.duration !== null) position = Math.min(position, sample.duration);
    return {videoId: sample.videoId, position, duration: sample.duration};
  }

  function emitProgress() {
    window.dispatchEvent(new CustomEvent('playback-progress', {detail: progress()}));
  }

  function animate() {
    frame = null;
    const current = progress();
    if (clock() - lastFrame >= 100 || !current ||
        (current.duration !== null && current.position >= current.duration)) {
      lastFrame = clock();
      emitProgress();
    }
    if (current && !sample.paused && current.position !== null &&
        (current.duration === null || current.position < current.duration)) {
      frame = requestAnimationFrame(animate);
    }
  }

  function publish(data) {
    bridgeSnapshot = data;
    window.playbackStatus.audioSnapshot = data;
    const tv = window.tvPlaybackControls;
    if (tv?.active()) data = {...data, connected: true, now_playing: tv.snapshot(), resumable_player: null};
    if (frame !== null) cancelAnimationFrame(frame);
    if (expiry !== null) clearTimeout(expiry);
    frame = null;
    expiry = null;
    const playing = data.connected && data.now_playing;
    sample = playing?.video_id ? {
      videoId: playing.video_id,
      position: number(playing.position) && playing.position >= 0 ? playing.position : null,
      duration: number(playing.duration) && playing.duration > 0 ? playing.duration : null,
      paused: playing.paused !== false, receivedAt: clock(),
      age: number(playing.snapshot_age) ? Math.max(0, playing.snapshot_age) : 0,
    } : null;
    window.playbackStatus.snapshot = data;
    lastFrame = clock();
    emitProgress();
    window.dispatchEvent(new CustomEvent('bridge-status', {detail: data}));
    // Paused/ended tracks do not animate. Still expire their displayed timing
    // if the next status request hangs, without adding another polling loop.
    if (sample && progress()) {
      expiry = setTimeout(() => {
        expiry = null;
        if (frame !== null) cancelAnimationFrame(frame);
        frame = null;
        emitProgress();
      }, Math.ceil((8 - sample.age) * 1000));
    }
    if (sample && !sample.paused && sample.position !== null && progress()) {
      frame = requestAnimationFrame(animate);
    }
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
    if (frame !== null) cancelAnimationFrame(frame);
    if (expiry !== null) clearTimeout(expiry);
    expiry = null;
    frame = sample = null;
    emitProgress();
  }

  window.playbackStatus = {snapshot: null, start, refresh, stop, progress};
  window.addEventListener('tv-playback-state', () => publish(bridgeSnapshot || {connected: false}));
  window.addEventListener('pagehide', stop);
  window.addEventListener('pageshow', start);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && timer !== null) refresh();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
