// Keep the module graph on the same build as this cache-busted entry point.
const version = new URL(import.meta.url).search;
const [{AudioPreviewSession}, {AudioVibeSampler}] = await Promise.all([
  import('./audio-preview-session.js' + version), import('./audio-vibe-sampler.js' + version),
]);

const root = document.querySelector('[data-audio-vibe-collection]');
const ratingRoot = document.querySelector('[data-audio-vibe-banner]');
const RUN = 'tuneconsole.audio-vibes.run.1', VIEW = 'tuneconsole.audio-vibes.view.1';
const OUTBOX = 'tuneconsole.audio-vibes.outbox.1';
const TAXONOMY = root.dataset.taxonomyVersion;
const RESTART_MESSAGE = 'Restart TuneConsole to load the listening session descriptions, then reload this page.';
let run = read(RUN), view = read(VIEW), receivedAt = Date.now();
let session = null, sampler = null, leader = false, acquiring = false;
let closed = false, release = null, flushing = false, retryAt = 0, timer = null;
let storageAvailable = true;
let lastActivityWrite = 0;
const requests = new Set();

function read(key, fallback = null) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch (error) { return fallback; }
}

function write(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch (error) {
    storageAvailable = false;
    console.warn('Browser storage is unavailable. Automatic audio sampling is paused.');
    return false;
  }
}

function playback() {
  if (window.tvPlaybackControls?.active()) return null;
  const data = window.playbackStatus?.audioSnapshot ?? window.playbackStatus?.snapshot;
  const playing = data?.connected && data.now_playing;
  return playing ? {...playing, at: receivedAt - Math.max(0, playing.snapshot_age || 0) * 1000} : null;
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
    return await response.json();
  } finally { clearTimeout(timeout); requests.delete(controller); }
}

function compatible(value) {
  return value?.taxonomy_version === TAXONOMY && Array.isArray(value.samples);
}

function merge(value, publish = false) {
  if (closed) return;
  if (!compatible(value)) {
    console.warn(RESTART_MESSAGE); return;
  }
  // Analysis can finish after playback has advanced. Its completed card still
  // belongs in the stack, even though the sampler now owns a different run.
  cards?.accept(value);
  if (value.run_id !== run?.id) return;
  const previous = view?.run_id === value.run_id ? view : null;
  // A slower GET or feedback response must not erase a newer sample result.
  if (previous && previous.samples.length > value.samples.length) {
    value = {...previous, choices: value.choices || previous.choices, feedback: value.feedback || previous.feedback};
  }
  view = value;
  for (const sample of value.samples) run.points[sample.point] = 'saved';
  if (publish) { write(VIEW, value); write(RUN, run); }
  // Finish saving before reserving the next point. This callback wakes a
  // background page without depending on its throttled interval.
  tick();
}

async function loadRun(id) {
  try {
    const playback = run?.id === id ? run : null;
    const query = playback ? '?video_id=' + encodeURIComponent(playback.video_id) : '';
    const value = await request('/audio-vibes/runs/' + encodeURIComponent(id) + query);
    // Before its first sample, this occurrence exists only in the sampler.
    // Its exact video still lets the server restore an earlier human answer.
    value.playback ||= playback;
    merge(value, true);
  }
  catch (error) { /* The outbox and card index survive a disconnected app. */ }
}

function changed(value) {
  const changedRun = run?.id !== value.id;
  run = value;
  if (changedRun) {
    view = null;
    loadRun(value.id);
  }
  if (cards) cards.accept({run_id: value.id,
    playback: {video_id: value.video_id, title: value.title, artist: value.artist},
    samples: [], prediction: null, choices: JSON.parse(ratingRoot.dataset.choices), taxonomy_version: TAXONOMY},
    {placeholder: true});
}

// Rating modules and their durable click queue are loaded only for developers.
const cards = ratingRoot ? await (await import('./audio-vibe-banner.js' + version)).createCards({
  root: ratingRoot, request, read, write, taxonomy: TAXONOMY}) : null;

function tick() {
  if (storageAvailable) sampler?.tick({collect: read(OUTBOX, []).length < 12});
}

function failed(value, capture = sampler?.active) {
  if (!capture) return;
  const failure = {run_id: capture.run.id, point: capture.point.key,
    attempt: capture.run.attempts[capture.point.key] || 1,
    phase: value.phase, message: String(value.message).slice(0, 512)};
  capture.run.failures = [...(capture.run.failures || []), failure].slice(-9);
  sampler.persist();
  // Typed, bounded metadata also reaches app.log so failed attempts can be
  // investigated alongside the successful measurements. Never send audio.
  request('/audio-vibes/failures', failure).catch(() => {});
}

async function flush() {
  if (!leader || flushing || closed || Date.now() < retryAt) return;
  flushing = true;
  try {
    let pending;
    while (!closed && (pending = read(OUTBOX, [])[0])) {
      try {
        const value = await request('/audio-vibes/samples', pending);
        write(OUTBOX, read(OUTBOX, []).filter(sample => sample.id !== pending.id));
        merge(value, true);
      } catch (error) {
        if (closed) return;
        if ([409, 413, 422].includes(error.status)) {
          write(OUTBOX, read(OUTBOX, []).filter(sample => sample.id !== pending.id));
          if (run?.id === pending.run_id) {
            run.points[pending.point] = 'failed'; write(RUN, run);
            failed({phase: 'saving', message: error.message}, {run, point: {key: pending.point}});
            loadRun(run.id);
          }
        } else {
          retryAt = Date.now() + 5000;
          return;
        }
      }
    }
  } finally { flushing = false; }
}

function collected(sample) {
  const outbox = read(OUTBOX, []);
  if (!write(OUTBOX, [...outbox, sample])) return;
  flush();
}

async function becomeLeader() {
  if (closed || leader || acquiring) return;
  if (!navigator.locks) { console.warn('Automatic sampling needs Chrome on the local app address.'); return; }
  acquiring = true;
  try {
    await navigator.locks.request('tuneconsole-audio-vibes', {ifAvailable: true}, async lock => {
      if (!lock || closed) return;
      leader = true;
      run = read(RUN);
      // Navigation interrupts an in-flight capture; retain completed/saved points.
      if (run) for (const [key, value] of Object.entries(run.points)) {
        if (['waiting', 'capturing', 'analysing'].includes(value)
            || (value === 'saving' && !read(OUTBOX, []).some(sample => sample.run_id === run.id && sample.point === key))) {
          run.points[key] = 'interrupted';
        }
      }
      if (run) delete run.activity;
      session = new AudioPreviewSession({playback, automatic: false, portName: 'tc-audio-collection',
        // Inference lives in the installed extension; the app need not carry
        // the checkout's optional research model files to collect measurements.
        extensionIds: root.dataset.extensionIds.split(',').filter(Boolean),
        changed(value) {
          const capture = sampler?.active;
          if (capture && capture.run === sampler.run) {
            const previous = capture.run.activity?.phase;
            if (['waiting', 'capturing', 'analysing'].includes(value.phase)) {
              capture.run.points[capture.point.key] = value.phase;
              capture.run.activity = {...value, point: capture.point.key};
            } else delete capture.run.activity;
            if (previous !== value.phase || Date.now() - lastActivityWrite >= 500) {
              lastActivityWrite = Date.now(); sampler.persist();
            }
          }
        }, result: value => sampler.completed(value), failure: failed});
      sampler = new AudioVibeSampler({session, playback, cached: run,
        save: value => write(RUN, value), changed, result: collected});
      if (run) loadRun(run.id);
      timer = setInterval(() => {
        tick();
        flush();
      }, 500);
      await new Promise(resolve => { release = resolve; });
      clearInterval(timer); session.destroy(); session = sampler = null; leader = false;
    });
  } finally { acquiring = false; }
}

window.addEventListener('app-before-auto-reload', event => {
  if (cards?.interacting()) event.preventDefault();
});
window.addEventListener('bridge-status', () => {
  receivedAt = Date.now();
  tick();
});
window.addEventListener('storage', event => {
  if (event.key === RUN) {
    const current = read(RUN);
    if (!current) return;
    if (leader && sampler?.run?.id === current.id) {
      sampler.run.dismissed = current.dismissed;
    } else if (!leader) changed(current);
  } else if (event.key === VIEW) merge(read(VIEW));
});
let election = setInterval(() => { becomeLeader(); cards?.tick(); }, 2000);
window.addEventListener('pagehide', () => {
  closed = true;
  if (cards) cards.stopped = true;
  clearInterval(election); clearInterval(timer);
  for (const controller of requests) controller.abort();
  release?.();
});
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  closed = false; run = read(RUN); view = read(VIEW);
  if (cards) cards.stopped = false;
  if (view?.run_id !== run?.id || !compatible(view)) view = null;
  election = setInterval(() => { becomeLeader(); cards?.tick(); }, 2000);
  if (run) { changed(run); loadRun(run.id); }
  cards?.restore(); cards?.tick(); becomeLeader();
});
if (view?.run_id !== run?.id || !compatible(view)) view = null;
// Another (possibly older, backgrounded) app tab may own sampling. Rebuild its
// identified current card now, without waiting for a sample or storage event.
if (run) { changed(run); loadRun(run.id); }
// Rehydrate completed cards from the server so a cached earlier heuristic does
// not win the race against its replacement and become this page's frozen card.
cards?.tick();
becomeLeader();
