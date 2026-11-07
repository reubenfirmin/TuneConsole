// Collection belongs to the extension. This document observes it and owns human drafts.
const version = new URL(import.meta.url).search;
const root = document.querySelector('[data-audio-vibe-collection]');
const ratingRoot = document.querySelector('[data-audio-vibe-banner]');
const RUN = 'tuneconsole.audio-vibes.run.1', VIEW = 'tuneconsole.audio-vibes.view.1';
const OUTBOX = 'tuneconsole.audio-vibes.outbox.1';
const TAXONOMY = root?.dataset.taxonomyVersion;
const diagnosticPage = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const diagnostics = new Map();
let run = read(RUN), view = read(VIEW), closed = false, port = null, retryAt = 0;
let receivedAt = Date.now(), signature = null, lastReceipt = null;
let status = {phase: 'connecting', message: 'Connecting to the TuneConsole extension…'};
const connecting = new Map(), requests = new Set(), loading = new Set(), reloads = new Set(), restored = new Set();

function diagnose(event, detail = {}, signature = JSON.stringify(detail), slot = event) {
  if (closed || diagnostics.get(slot) === signature) return;
  diagnostics.set(slot, signature);
  // Best-effort state transitions, never polling receipts, song titles or audio.
  // Keep diagnostic delivery independent of collection and human draft requests.
  try {
    fetch('/audio-vibes/diagnostics', {method: 'POST', keepalive: true,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        page_id: diagnosticPage, asset_version: new URL(import.meta.url).searchParams.get('v') || '',
        path: location.pathname.slice(0, 256), event, ...detail})}).catch(() => {});
  } catch (error) {}
}
diagnose('started', {runtime_available: !!globalThis.chrome?.runtime?.connect,
  locks_available: !!navigator.locks, secure_context: window.isSecureContext});

function read(key, fallback = null) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch (error) { return fallback; }
}
function write(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch (error) { console.warn('Browser storage is unavailable. Keep this page open to retain unsaved labels.'); return false; }
}
async function request(url, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  requests.add(controller);
  try {
    const response = await fetch(url, {signal: controller.signal,
      ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {})});
    if (!response.ok) {
      const error = Error(`Vibe request failed (HTTP ${response.status}).`);
      error.status = response.status;
      throw error;
    }
    const value = await response.json();
    diagnostics.delete('request:' + url.split('?')[0].replace(/\/runs\/[^/]+$/, '/runs/:id'));
    return value;
  } catch (error) {
    if (!closed) diagnose('request_failed', {api_status: error.status || 0,
      detail: `${body ? 'POST' : 'GET'} ${url.split('?')[0]}: ${error.message}`.slice(0, 256)}, String(error.status || 0),
      'request:' + url.split('?')[0].replace(/\/runs\/[^/]+$/, '/runs/:id'));
    throw error;
  } finally { clearTimeout(timeout); requests.delete(controller); }
}
function compatible(value) {
  return value?.taxonomy_version === TAXONOMY && Array.isArray(value.samples);
}
function observe(playback) {
  return cards?.accept({run_id: playback.id,
    playback: {video_id: playback.video_id, title: playback.title, artist: playback.artist},
    samples: [], prediction: null, collection_status: status,
    choices: JSON.parse(ratingRoot.dataset.choices), taxonomy_version: TAXONOMY}, {placeholder: true});
}
function merge(value) {
  if (closed || !compatible(value)) return;
  if (value.run_id === run?.id) value.collection_status = status;
  cards?.accept(value);
  if (value.run_id !== run?.id) return;
  const previous = view?.run_id === value.run_id ? view : null;
  if (previous && previous.samples.length > value.samples.length)
    value = {...previous, choices: value.choices || previous.choices, feedback: value.feedback || previous.feedback};
  view = value;
  write(VIEW, value);
}
async function loadRun(id, playback = null) {
  if (loading.has(id)) { reloads.add(id); return; }
  loading.add(id);
  try {
    playback ||= run?.id === id ? run : null;
    // Admit playback before starting its GET. Later responses only update that
    // existing occurrence, including when another tab dismisses it meanwhile.
    if (playback) await observe(playback);
    const query = playback ? '?video_id=' + encodeURIComponent(playback.video_id) : '';
    const value = await request('/audio-vibes/runs/' + encodeURIComponent(id) + query);
    value.playback ||= playback;
    merge(value);
    restored.add(id);
  } catch (error) { /* Durable extension metadata and the human card index retry independently. */ }
  finally {
    loading.delete(id);
    if (reloads.delete(id) && !closed) loadRun(id, playback);
  }
}
function changed(value) {
  if (!value?.id) return;
  const next = JSON.stringify([value.id, value.points]);
  run = {...value, collection_status: status};
  write(RUN, run);
  if (next !== signature) { signature = next; loadRun(run.id); }
  observe(run);
}
const cards = await (async () => {
  if (!ratingRoot) return null;
  try {
    const cards = await (await import('./audio-vibe-banner.js' + version)).createCards({
      root: ratingRoot, request, read, write, taxonomy: TAXONOMY, diagnose});
    diagnose('cards_ready');
    return cards;
  } catch (error) {
    diagnose('cards_failed', {detail: String(error.message).slice(0, 256)});
    return null;
  }
})();

function restore() {
  try { port?.postMessage({type: 'restore', run: read(RUN), samples: read(OUTBOX, [])}); }
  catch (error) { /* No metadata is removed until the extension acknowledges storage. */ }
}
function receive(data) {
  if (data.type === 'restored') {
    const ids = new Set(data.ids);
    write(OUTBOX, read(OUTBOX, []).filter(sample => !ids.has(sample.id)));
  } else if (data.type === 'collection' && data.protocol === 1) {
    status = data.status || null;
    const recent = new Set([data.run?.id, ...(data.recent || []).map(previous => previous.id)]);
    for (const id of restored) if (!recent.has(id)) restored.delete(id);
    if (data.run) changed(data.run);
    for (const previous of data.recent || []) if (!restored.has(previous.id)) loadRun(previous.id, previous);
    if (data.receipt && data.receipt.id !== lastReceipt) {
      lastReceipt = data.receipt.id;
      loadRun(data.receipt.run_id);
    }
  }
}
function disconnect(candidate) {
  clearTimeout(connecting.get(candidate));
  connecting.delete(candidate);
  // A local Port.disconnect() only notifies the other end. Always settle our
  // own attempt here, including timeout, rejection and competing candidates.
  try { candidate.disconnect(); } catch (error) {}
}
function unavailable() {
  if (closed || port || connecting.size) return;
  if (!status?.update) status = {phase: 'unavailable',
    message: 'Reload the TuneConsole extension, then reload this page to resume audio checks.'};
  diagnose('extension_unavailable', {reason: status.update ? 'update_required' : 'disconnected'});
  if (run) changed(run);
  playbackHint();
}
function connect() {
  if (closed || !root || port || connecting.size || Date.now() < retryAt) return;
  retryAt = Date.now() + 2000;
  if (!globalThis.chrome?.runtime?.connect) {
    status = {phase: 'unavailable', message: 'Reload the TuneConsole extension, then reload this page in Chrome or Chromium.'};
    diagnose('extension_unavailable', {reason: 'runtime_unavailable'});
    if (run) changed(run);
    return;
  }
  diagnose('extension_connecting');
  for (const id of root.dataset.extensionIds.split(',').filter(Boolean)) {
    try {
      const candidate = chrome.runtime.connect(id, {name: 'tc-audio-collection'});
      connecting.set(candidate, setTimeout(() => { disconnect(candidate); unavailable(); }, 3000));
      candidate.onMessage.addListener(data => {
        if (closed || (!connecting.has(candidate) && candidate !== port)) return;
        if (data?.type === 'ready' && data.protocol === 1 && !port) {
          clearTimeout(connecting.get(candidate));
          port = candidate;
          connecting.delete(candidate);
          for (const other of connecting.keys()) disconnect(other);
          status = null;
          root.dataset.extensionVersion = data.extension_version || 'unknown';
          root.dataset.collectionProtocol = String(data.protocol);
          diagnose('extension_ready', {extension_id: id,
            extension_version: root.dataset.extensionVersion, protocol: root.dataset.collectionProtocol});
          if (run) changed(run);
          restore();
        } else if (data?.type === 'ready' && data.protocol !== 1) {
          status = {phase: 'unavailable', update: true,
            message: 'Update to TuneConsole extension 0.1.24 or later, then reload this page to resume audio checks.'};
          root.dataset.extensionVersion = data.extension_version || 'unknown';
          root.dataset.collectionProtocol = String(data.protocol ?? 'legacy');
          diagnose('extension_legacy', {extension_id: id,
            extension_version: root.dataset.extensionVersion, protocol: root.dataset.collectionProtocol});
          disconnect(candidate);
          if (run) changed(run);
          playbackHint();
          unavailable();
        }
        else if (candidate === port) receive(data);
      });
      candidate.onDisconnect.addListener(() => {
        void chrome.runtime.lastError;
        clearTimeout(connecting.get(candidate));
        connecting.delete(candidate);
        if (port === candidate) port = null;
        unavailable();
      });
    } catch (error) {}
  }
  unavailable();
}
function playbackHint() {
  const data = window.playbackStatus?.audioSnapshot ?? window.playbackStatus?.snapshot;
  const playing = data?.connected && data.now_playing;
  const reason = port ? 'extension_owned' : status?.phase !== 'unavailable' ? 'connecting'
    : window.tvPlaybackControls?.active() ? 'tv_playback' : !data ? 'no_status'
    : !data.connected ? 'disconnected' : !playing?.video_id ? 'no_track'
    : playing.paused !== false ? 'paused' : (playing.snapshot_age || 0) > 6 ? 'stale' : 'observed';
  diagnose('playback', {reason, ...(playing ? {
    video_id: playing.video_id, paused: playing.paused,
    snapshot_age: Math.min(86400, Math.max(0, playing.snapshot_age || 0))} : {}),
    ...(data ? {connected: data.connected} : {})}, JSON.stringify([reason, playing?.video_id]));
  if (reason !== 'observed') return;
  // Labels may be entered before the extension reconnects. Its restore handshake
  // adopts this occurrence only when it does not already own one.
  const replay = run?.video_id === playing.video_id && playing.position < 5
    && run.last_position > Math.max(30, (playing.duration || Infinity) * .8);
  if (run?.video_id !== playing.video_id || replay || receivedAt - run.updated_at > 86400000)
    changed({id: crypto.randomUUID(), ...playing,
    points: {}, attempts: {}, retry_after: {}, last_position: playing.position, updated_at: receivedAt});
  else if (run.updated_at !== receivedAt) {
    run.last_position = playing.position;
    run.updated_at = receivedAt;
    write(RUN, run);
  }
}
function tick() {
  connect(); playbackHint(); cards?.tick();
  if (port && read(OUTBOX, []).length) restore();
  if (run && !restored.has(run.id)) loadRun(run.id);
}
window.addEventListener('bridge-status', () => { receivedAt = Date.now(); playbackHint(); });
window.addEventListener('app-before-auto-reload', event => { if (cards?.interacting()) event.preventDefault(); });
window.addEventListener('storage', event => {
  if (event.key === RUN && !port) { const value = read(RUN); if (value) changed(value); }
  if (event.key === VIEW) { const value = read(VIEW); if (value) merge(value); }
});
let timer = setInterval(tick, 2000);
window.addEventListener('pagehide', () => {
  closed = true; clearInterval(timer);
  if (cards) cards.stopped = true;
  for (const controller of requests) controller.abort();
  port?.disconnect(); port = null;
  for (const candidate of connecting.keys()) disconnect(candidate);
});
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  closed = false;
  if (cards) cards.stopped = false;
  timer = setInterval(tick, 2000);
  tick();
});
if (view?.run_id !== run?.id || !compatible(view)) view = null;
if (run) { changed(run); loadRun(run.id); }
tick();
