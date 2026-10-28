// Audio stays in the YTM tab. Only bounded predictions and frozen embeddings leave.
function createAudioPreview({appOrigin, pickTab}) {
  let active = null;
  let collectionAvailable;
  function hasCollectionModel() {
    // Public extension archives currently omit the optional research runtime.
    // Probe local packaged assets before allowing unattended audio capture.
    collectionAvailable ||= (async () => {
      try {
        const response = await fetch(chrome.runtime.getURL('audio_model/vendor/bundle.json'));
        if (!response.ok) return false;
        const bundle = await response.json();
        return bundle.schema === 1 && ['musicnn/model.json', 'musicnn/group1-shard1of1.bin'].every(path =>
          bundle.files?.some(file => file.path === path && /^[a-f0-9]{64}$/.test(file.sha256)));
      } catch (error) { return false; }
    })();
    return collectionAvailable;
  }
  function metadata(value) {
    const keys = ['id', 'descriptors', 'audio_attributes', 'derivation_version', 'wall_ms', 'sample_count', 'prediction',
      'analysis_location', 'audio_transferred_to_app', 'capture_method', 'pcm_reader',
      'realtime_audio_context_used', 'trigger', 'user_activation_at_start', 'capture_sample_rate',
      'capture_seconds', 'recorded_seconds', 'source_position_at_start', 'song_position_at_start',
      'source_playback', 'source_identity_verified', 'raw_audio_retained'];
    if (!value || value.analysis_location !== 'youtube_tab' || value.audio_transferred_to_app !== false
        || value.sample_count !== 160000 || value.raw_audio_retained !== false
        || !value.prediction?.scores || Object.keys(value.prediction.scores).length !== 50
        || Object.values(value.prediction.scores).some(n => !Number.isFinite(n) || n < 0 || n > 1))
      throw Error('Invalid tab-local analysis result.');
    const result = Object.fromEntries(keys.filter(key => key in value).map(key => [key, value[key]]));
    const embedding = result.prediction.embedding;
    if (embedding != null) {
      if (typeof embedding !== 'object' || Array.isArray(embedding)
          || Object.keys(embedding).sort().join(',') !== 'graph_policy,layer,patches,pooling,values,version'
          || embedding.version !== 'musicnn-embedding-1' || embedding.layer !== 'model/dense/BiasAdd'
          || embedding.graph_policy !== 'restore-fused-preactivation-1' || embedding.pooling !== 'mean_full_patches'
          || !Number.isInteger(embedding.patches) || embedding.patches < 1 || embedding.patches > 8
          || !Array.isArray(embedding.values) || embedding.values.length !== 200
          || embedding.values.some(n => typeof n !== 'number' || !Number.isFinite(n) || Math.abs(n) > 1e6)
          || result.prediction.model !== 'msd-musicnn-1'
          || typeof result.prediction.model_version !== 'string' || !result.prediction.model_version
          || !['musicnn/model.json', 'musicnn/group1-shard1of1.bin'].every(path =>
            result.prediction.bundle?.files?.some(file => file.path === path && /^[a-f0-9]{64}$/.test(file.sha256))))
        throw Error('Invalid frozen music embedding.');
    }
    const forbidden = new Set(['pcm', 'samples', 'embeddings', 'mel', 'spectrogram', 'audio', 'buffer']);
    function inspect(item, depth = 0, path = '') {
      if (depth > 12 || (typeof item === 'string' && item.length > 1024)
          || (typeof item === 'number' && !Number.isFinite(item))) throw Error('Invalid analysis metadata.');
      if (!item || typeof item !== 'object') return;
      if (ArrayBuffer.isView(item) || item instanceof ArrayBuffer
          || (Array.isArray(item) && item.length > (path === 'prediction.embedding.values' ? 200 : 64)))
        throw Error('Audio features cannot leave the music tab.');
      for (const [key, child] of Object.entries(item)) {
        if (forbidden.has(key)) throw Error('Audio features cannot leave the music tab.');
        if (key === 'embedding' && path !== 'prediction') throw Error('Unexpected embedding location.');
        inspect(child, depth + 1, path ? path + '.' + key : key);
      }
    }
    inspect(result);
    if (JSON.stringify(result).length > 32768) throw Error('Analysis metadata exceeded its limit.');
    return result;
  }
  function allowed(sender) {
    try {
      const url = new URL(sender?.url), app = new URL(appOrigin);
      return sender.frameId === 0 && url.protocol === app.protocol && url.port === app.port &&
        [app.hostname, 'localhost'].includes(url.hostname);
    } catch (e) { return false; }
  }
  function connect(port) {
    if (!['tc-audio-preview', 'tc-audio-collection'].includes(port.name) || !allowed(port.sender)) {
      port.disconnect(); return;
    }
    let capture = null, closed = false, timer = null, job = null;
    let ready = port.name === 'tc-audio-preview';
    const send = message => {
      if (closed) return false;
      try { port.postMessage(message); return true; }
      catch (e) { close(); return false; }
    };
    function cancel() {
      clearTimeout(timer);
      const source = capture;
      capture = null;
      source?.disconnect();
      if (active === job) active = null;
      job = null;
    }
    function close() { closed = true; cancel(); }
    port.onDisconnect.addListener(close);
    port.onMessage.addListener(async message => {
      if (message?.type === 'stop') { cancel(); return; }
      if (!ready || message?.type !== 'capture' || typeof message.id !== 'string' || message.id.length > 80) return;
      if (active) { send({id: message.id, error: 'Another audio preview is already running. Stop it first.'}); return; }
      const request = job = {port};
      active = request;
      const id = message.id;
      const waitingMs = Number.isFinite(message.not_before) && message.not_before >= 0 && message.not_before <= 86400
        ? message.not_before * 1000 : 0;
      try {
        const tab = await pickTab();
        if (closed || job !== request) return;
        if (!tab) throw Error('Audio capture could not find a YouTube Music tab [music_tab_unavailable].');
        // Install on demand too, so an already-open player works after a reload.
        await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['audio_preview_capture.js']});
        if (closed || job !== request) return;
        const source = capture = chrome.tabs.connect(tab.id, {name: 'tc-audio-preview-capture'});
        source.onMessage.addListener(async data => {
          if (capture !== source || closed || data?.id !== id) return;
          if (data.type === 'result') {
            let owner;
            try { owner = await pickTab(); }
            catch (error) { if (capture === source) { send({id, error: 'The music player is unavailable.'}); cancel(); } return; }
            if (capture !== source || closed) return;
            if (owner?.id !== tab.id) { send({id, error: 'The playing music tab changed.'}); cancel(); return; }
            try { send({id, type: 'result', result: metadata(data.result)}); }
            catch (error) { send({id, error: error.message}); }
            cancel();
          } else if (data.type === 'waiting') {
            send({id, type: 'waiting'});
          } else if (data.type === 'analysing') {
            send({id, type: 'analysing'});
            clearTimeout(timer);
            timer = setTimeout(() => { send({id, error: 'Audio analysis timed out.'}); cancel(); }, 32000);
          } else if (data.error) {
            // Keep source-media failures distinct from worker tab discovery.
            // State flags identify a wrong/loading tab without exporting URLs.
            const state = `tab=${tab.id},audible=${+!!tab.audible},muted=${+!!tab.mutedInfo?.muted},` +
              `loading=${+(tab.status === 'loading')},pending=${+!!tab.pendingUrl}`;
            send({id, error: `${String(data.error).slice(0, 380)} [${state}]`.slice(0, 500)});
            cancel();
          }
          else if (data.type === 'progress') {
            if (data.seconds === 0) {
              clearTimeout(timer);
              timer = setTimeout(() => { send({id, error: 'Audio capture timed out.'}); cancel(); }, 16000);
            }
            send({id, type: 'progress', seconds: data.seconds});
          }
          else { send({id, error: 'Only analysis metadata may leave the music tab.'}); cancel(); }
        });
        source.onDisconnect.addListener(() => {
          if (capture === source) {
            send({id, error: 'Audio capture disconnected. Reload the TuneConsole extension and try again.'});
            cancel();
          }
        });
        timer = setTimeout(() => { send({id, error: 'Audio capture timed out.'}); cancel(); }, 16000 + waitingMs);
        source.postMessage({type: 'capture', id, video_id: message.video_id,
          ...(waitingMs ? {not_before: message.not_before} : {})});
      } catch (error) {
        if (!closed && job === request) { send({id, error: String(error.message || error)}); cancel(); }
      }
    });
    if (ready) send({type: 'ready'});
    else hasCollectionModel().then(available => {
      ready = available;
      send({type: available ? 'ready' : 'unavailable'});
    });
  }
  return {connect};
}
