// The extension owns scheduling and durable metadata delivery. App ports only observe.
function createAudioCollection({appOrigin, capture, storage = chrome.storage?.local,
  clock = Date.now, fetcher = globalThis.fetch, interval = globalThis.setInterval,
  extensionVersion = globalThis.chrome?.runtime?.getManifest?.().version ?? null}) {
  const KEY = 'tuneconsole.audio-collection.1', LIMIT = 12;
  const observers = new Set();
  let state = {run: null, outbox: [], recent: []}, playback = null, sampler;
  let ready = false, available = false, durable = false, flushing = false, retryAt = 0, failures = 0;
  let writing = Promise.resolve(), status = null, current = null, port = null, receipt = null;

  function metadataOnly(value, limit) {
    const forbidden = new Set(['pcm', 'audio', 'buffer', 'samples', 'embeddings', 'mel', 'spectrogram']);
    function inspect(item, depth = 0, path = '') {
      if (depth > 12 || (typeof item === 'number' && !Number.isFinite(item))
          || (typeof item === 'string' && item.length > 1024)) return false;
      if (!item || typeof item !== 'object') return true;
      if (ArrayBuffer.isView(item) || item instanceof ArrayBuffer
          || (Array.isArray(item) && item.length > (path === 'embedding.values' ? 200 : 64))) return false;
      return Object.entries(item).every(([key, child]) => !forbidden.has(key)
        && inspect(child, depth + 1, path ? path + '.' + key : key));
    }
    try { return inspect(value) && JSON.stringify(value).length <= limit; }
    catch (error) { return false; }
  }
  function importable(sample) {
    return typeof sample?.id === 'string' && sample.id.length === 36
      && typeof sample.run_id === 'string' && sample.run_id.length === 36
      && sample.analysis_location === 'youtube_tab' && sample.audio_transferred_to_app === false
      && sample.raw_audio_retained === false && sample.tags && sample.traits && metadataOnly(sample, 65536);
  }

  function publish() {
    const value = {type: 'collection', protocol: 1, extension_version: extensionVersion, run: state.run,
      recent: state.recent, status, pending: state.outbox.length, receipt};
    for (const observer of observers) {
      try { observer.postMessage(value); } catch (error) { observers.delete(observer); }
    }
  }
  function persist() {
    const snapshot = structuredClone(state);
    const task = writing.then(async () => {
      if (!storage) throw Error('Extension storage is unavailable.');
      await storage.set({[KEY]: snapshot});
      durable = true;
    });
    writing = task.catch(() => {
      durable = false;
      status = {phase: 'unavailable', message: 'Audio checks are paused. You can still label this song.'};
      publish();
    });
    return task;
  }
  function remember(run) {
    state.recent = [...state.recent.filter(value => value.id !== run.id),
      {id: run.id, video_id: run.video_id, title: run.title, artist: run.artist}].slice(-24);
  }
  function changed(run) {
    state.run = run;
    remember(run);
    run.collection_status = status;
    persist().catch(() => {});
    publish();
  }
  function validPlaying() {
    if (!playback?.video_id || playback.paused !== false || playback.muted || playback.tab_muted
        || playback.volume === 0 || !Number.isFinite(playback.position)
        || clock() - playback.at > 6000 || clock() < playback.at - 100) return null;
    return {...playback, position: playback.position + Math.max(0, clock() - playback.at) / 1000};
  }
  function stop() {
    current = null;
    port?.postMessage({type: 'stop'});
  }
  const session = {get port() { return ready && available ? port : null; },
    get current() { return current; }, validPlaying, stop,
    schedule(seconds) {
      const playing = validPlaying();
      if (current || !playing || !this.port || capture.previewOpen) return;
      current = {token: crypto.randomUUID(), playing, analysing: false};
      port.postMessage({type: 'capture', id: current.token, video_id: playing.video_id, not_before: seconds});
    }};
  function receive(data) {
    if (data.type === 'ready' || data.type === 'unavailable') {
      available = data.type === 'ready';
      status = available ? null : {phase: 'unavailable',
        message: 'Audio checks need the extension’s audio model. Reload the extension after installing it.'};
      publish(); tick(); return;
    }
    const job = current;
    if (!job || data.id !== job.token) return;
    if (data.error) {
      current = null;
      const active = sampler?.active;
      if (active) {
        const failure = {run_id: active.run.id, point: active.point.key,
          attempt: active.run.attempts[active.point.key] || 1,
          phase: job.analysing ? 'analysing' : 'capturing', message: String(data.error).slice(0, 512)};
        active.run.failures = [...(active.run.failures || []), failure].slice(-9);
        // Diagnostics are best effort; samples have their own durable queue.
        post('/audio-vibes/failures', failure).catch(() => {});
      }
      tick(); return;
    }
    if (data.type === 'analysing') job.analysing = true;
    if (['waiting', 'progress', 'analysing'].includes(data.type)) {
      const active = sampler?.active;
      if (active) {
        active.run.points[active.point.key] = data.type === 'progress' ? 'capturing' : data.type;
        sampler.persist();
      }
    } else if (data.type === 'result') {
      current = null;
      const value = data.result;
      if (!value?.prediction?.moods?.traits) { tick(); return; }
      sampler.completed({...value, observed_playback: job.playing,
        observed_position_at_start: job.playing.position});
    }
  }
  async function post(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetcher(appOrigin + path, {method: 'POST', signal: controller.signal,
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      if (!response.ok) {
        const error = Error('Audio delivery failed (HTTP ' + response.status + ').');
        error.status = response.status;
        throw error;
      }
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  async function flush() {
    if (!ready || flushing || clock() < retryAt || !state.outbox.length) return;
    flushing = true;
    try {
      while (state.outbox.length) {
        // Save before sending. A worker stopped after server acceptance retries
        // the same UUID, and only an explicit matching receipt removes it.
        await persist();
        const sample = state.outbox[0];
        const response = await post('/audio-vibes/samples', sample);
        if (response.run_id !== sample.run_id || !response.samples?.some(value => value.id === sample.id))
          throw Error('Audio delivery did not acknowledge this check.');
        const remaining = state.outbox.filter(value => value.id !== sample.id);
        const previous = state.outbox;
        state.outbox = remaining;
        if (state.run?.id === sample.run_id) state.run.points[sample.point] = 'saved';
        try { await persist(); }
        catch (error) { state.outbox = previous; throw error; }
        failures = 0; retryAt = 0;
        receipt = {run_id: sample.run_id, id: sample.id};
        status = null;
        publish();
      }
    } catch (error) {
      failures++;
      retryAt = clock() + Math.min(60000, 5000 * 2 ** Math.min(failures - 1, 4));
      const sample = state.outbox[0];
      if (sample && failures === 1) {
        const failure = {run_id: sample.run_id, point: sample.point,
          attempt: 1, phase: 'saving', message: String(error.message).slice(0, 512)};
        if (state.run?.id === sample.run_id) {
          state.run.failures = [...(state.run.failures || []), failure].slice(-9);
          persist().catch(() => {});
        }
        post('/audio-vibes/failures', failure).catch(() => {});
      }
      if (durable) status = error.status >= 400 && error.status < 500
        ? {phase: 'unavailable', message: 'Audio checks are paused. You can still label this song.'}
        : {phase: 'saving', message: 'Audio checks are saved for later.'};
      publish();
    } finally { flushing = false; }
    tick(false);
  }
  function collected(sample) {
    // Capacity was reserved before capture. Never discard completed metadata
    // when the backend or extension storage becomes unavailable mid-capture.
    state.outbox.push(sample);
    persist().then(flush).catch(() => {});
    publish();
  }
  function tick(deliver = true) {
    if (!ready || !sampler) return;
    sampler.tick({collect: durable && available && !capture.previewOpen && state.outbox.length < LIMIT});
    if (deliver) flush();
  }
  const loaded = (async () => {
    try {
      const saved = (await storage?.get(KEY))?.[KEY];
      if (saved) {
        if (!Array.isArray(saved.outbox) || saved.outbox.length > LIMIT || !saved.outbox.every(importable)
            || !Array.isArray(saved.recent) || saved.recent.length > 24)
          throw Error('Invalid extension collection state.');
        state = saved;
      }
      if (state.run) for (const [point, value] of Object.entries(state.run.points)) {
        if (['waiting', 'capturing', 'analysing'].includes(value)
            || (value === 'saving' && !state.outbox.some(sample => sample.run_id === state.run.id && sample.point === point)))
          state.run.points[point] = 'interrupted';
      }
      await persist();
      sampler = new TuneConsoleAudioSampler.AudioVibeSampler({session, playback: () => playback,
        cached: state.run, clock, save() {}, changed, result: collected});
      ready = true;
      port = capture.openCollection(receive);
      capture.onCollectionChanged(() => { publish(); tick(); });
      interval?.(tick, 500);
      publish(); tick();
    } catch (error) {
      status = {phase: 'unavailable', message: 'Audio checks are paused. You can still label this song.'};
      publish();
    }
  })();
  function observe(frame) {
    if (!['play', 'now-heartbeat', 'player-closed'].includes(frame?.type)) return;
    if (frame.type === 'player-closed') playback = null;
    else if (typeof frame.videoId === 'string' && frame.videoId.length <= 128) playback = {
      video_id: frame.videoId, title: String(frame.title || '').slice(0, 512),
      artist: String(frame.artist || '').slice(0, 512), metadata_verified: frame.metadataVerified === true,
      paused: frame.paused, position: Number.isFinite(frame.position) && frame.position >= 0
        && frame.position <= 86400 ? frame.position : null,
      duration: Number.isFinite(frame.duration) && frame.duration > 0 && frame.duration <= 86400 ? frame.duration : null,
      muted: frame.muted, tab_muted: frame.tabMuted, volume: frame.volume, at: clock()};
    tick();
  }
  function connect(observer) {
    if (observer.name !== 'tc-audio-collection' || !capture.allowed(observer.sender)) return false;
    observers.add(observer);
    observer.onDisconnect.addListener(() => observers.delete(observer));
    observer.onMessage.addListener(async message => {
      await loaded;
      if (message?.type !== 'restore' || !ready) return;
      // Import pre-upgrade app metadata only, retaining its UUID until durable
      // extension storage acknowledges it. Human drafts remain in the app.
      const imported = [];
      for (const sample of (Array.isArray(message.samples) ? message.samples.slice(0, LIMIT) : [])) {
        if (!importable(sample)) continue;
        if (state.outbox.some(value => value.id === sample.id)) imported.push(sample.id);
        else if (state.outbox.length < LIMIT - (current ? 1 : 0)) {
          state.outbox.push(sample); imported.push(sample.id);
        }
      }
      const legacy = message.run;
      const canAdopt = !state.run || (legacy?.video_id === state.run.video_id
        && legacy.id !== state.run.id && clock() - legacy.updated_at < 86400000
        && !state.restored_from_app && !current?.analysing
        && !Object.values(state.run.points).some(value => ['saved', 'saving'].includes(value)));
      if (canAdopt && typeof legacy?.id === 'string' && legacy.id.length === 36
          && typeof legacy?.video_id === 'string' && legacy.video_id.length <= 128
          && legacy?.points && metadataOnly(legacy, 16384)) {
        stop(); sampler.active = null;
        state.recent = state.recent.filter(value => value.id !== state.run?.id);
        state.run = structuredClone(message.run);
        state.restored_from_app = true;
        sampler.run = state.run;
        for (const [point, value] of Object.entries(state.run.points))
          if (!['saved', 'too_short'].includes(value)
              && !state.outbox.some(sample => sample.run_id === state.run.id && sample.point === point))
            state.run.points[point] = 'interrupted';
        remember(state.run);
      }
      try {
        await persist();
        observer.postMessage({type: 'restored', ids: imported});
        publish(); tick();
      } catch (error) { /* The app retains its original outbox until acknowledgement. */ }
    });
    observer.postMessage({type: 'ready', protocol: 1, extension_version: extensionVersion});
    publish();
    return true;
  }
  return {observe, connect, tick, loaded};
}

globalThis.TuneConsoleAudioCollection = {createAudioCollection};
