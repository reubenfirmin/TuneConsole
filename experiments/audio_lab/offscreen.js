import {Sampler, POLICY} from './sampling.js';

let session = null;
let generation = 0;
const publish = message => chrome.runtime.sendMessage({target: 'background', type: 'capture-event', ...message})
  .catch(() => {});

async function stop() {
  generation++;
  const s = session;
  session = null;
  if (!s) return;
  clearInterval(s.watchdog);
  clearTimeout(s.settling);
  clearTimeout(s.analysisTimeout);
  s.sampler?.discard();
  s.worker?.terminate();
  s.node?.disconnect();
  s.source?.disconnect();
  s.stream?.getTracks().forEach(track => track.stop());
  await s.context?.close();
}

async function start(message) {
  await stop();
  const current = generation;
  const s = {id: message.session, lastState: 0, busy: false, count: 0, skipped: 0};
  session = s;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: {mandatory: {
      chromeMediaSource: 'tab', chromeMediaSourceId: message.streamId}}, video: false});
    if (current !== generation) { stream.getTracks().forEach(t => t.stop()); return {ok: false}; }
    s.stream = stream;
    s.context = new AudioContext();
    s.source = s.context.createMediaStreamSource(stream);
    // Tab capture suppresses the original output. Restore it before analysis starts.
    s.source.connect(s.context.destination);
    await s.context.resume();
    await s.context.audioWorklet.addModule('pcm_worklet.js');
    if (current !== generation) return {ok: false};
    s.worker = message.model ? new Worker('model_worker.js') : new Worker('analysis_worker.js', {type: 'module'});
    s.node = new AudioWorkletNode(s.context, 'reference-pcm');
    const silent = s.context.createGain();
    silent.gain.value = 0;
    s.source.connect(s.node).connect(silent).connect(s.context.destination);
    s.sampler = new Sampler(s.context.sampleRate, window => {
      if (s.busy) { window.samples.fill(0); s.skipped++; return; }
      s.busy = true;
      s.analysisTimeout = setTimeout(() => {
        if (session === s) { publish({session: s.id, status: 'error', error: 'Analysis exceeded the 30 second experiment limit'}); stop(); }
      }, 30000);
      // Require a fresh player state after the last audio frame, so a transition
      // racing the window's tail cannot silently acquire the old identity.
      s.settling = setTimeout(() => {
        if (session !== s || s.token !== window.token || s.lastState < window.endAt) {
          window.samples.fill(0); clearTimeout(s.analysisTimeout); s.busy = false; s.skipped++; return;
        }
        const id = `${s.id}/${window.index}`;
        // The fresh tail state has now validated this complete window. Later
        // transport changes cannot alter its samples or source identity. Keep
        // its analysis through pause/end; stop or identity/permission changes
        // still destroy the session and terminate its worker.
        const {samples, ...coverage} = window;
        delete coverage.endAt;
        s.pending = {id, ...coverage, preprocessing_ms: 0};
        if (message.model && s.context.sampleRate !== 16000) {
          // Browser's band-limited offline resampler; the audible route keeps
          // the device sample rate and never waits for model preprocessing.
          const preprocessingStarted = performance.now();
          const offline = new OfflineAudioContext(1, Math.ceil(samples.length * 16000 / s.context.sampleRate), 16000);
          const buffer = offline.createBuffer(1, samples.length, s.context.sampleRate);
          buffer.copyToChannel(samples, 0);
          samples.fill(0);
          const source = offline.createBufferSource(); source.buffer = buffer; source.connect(offline.destination); source.start();
          offline.startRendering().then(rendered => {
            buffer.getChannelData(0).fill(0);
            if (session !== s || s.pending?.id !== id) {
              rendered.getChannelData(0).fill(0);
              clearTimeout(s.analysisTimeout); s.pending = null; s.busy = false; s.skipped++;
              return;
            }
            s.pending.preprocessing_ms = performance.now() - preprocessingStarted;
            const mono = new Float32Array(rendered.getChannelData(0));
            rendered.getChannelData(0).fill(0);
            s.worker.postMessage({id, samples: mono, rate: 16000}, [mono.buffer]);
          }).catch(() => {
            buffer.getChannelData(0).fill(0);
            clearTimeout(s.analysisTimeout); s.pending = null; s.busy = false; s.skipped++;
          });
        } else s.worker.postMessage({id, samples, rate: s.context.sampleRate}, [samples.buffer]);
      }, 600);
      window.endAt = Date.now();
    });
    s.worker.onmessage = ({data}) => {
      if (session !== s || !s.pending || data.id !== s.pending.id) return;
      clearTimeout(s.analysisTimeout);
      if (!data.error) {
        const {token, epoch, ...coverage} = s.pending;
        s.count++;
        publish({session: s.id, status: 'sample', result: {schema: 1, policy: POLICY,
          ...coverage, ...data, sample_rate: message.model ? 16000 : s.context.sampleRate,
          capture_sample_rate: s.context.sampleRate,
          interval_basis: 'player_clock_estimate', clock_tolerance_s: .3,
          kind: 'measurement', recording_association: null, intensity: null,
          model_version: data.prediction?.model_version ?? null, raw_audio_retained: false}});
      } else {
        s.skipped++;
        if (data.error) { publish({session: s.id, status: 'error', error: data.error}); stop(); }
      }
      s.pending = null;
      s.busy = false;
    };
    s.worker.onerror = () => { publish({session: s.id, status: 'error', error: 'Analysis worker failed'}); stop(); };
    s.node.port.onmessage = ({data}) => {
      if (session === s) s.sampler.push(data.samples, data.time, data.token);
      data.samples.fill(0);
      s.node.port.postMessage({ack: true});
    };
    stream.getAudioTracks()[0].onended = () => {
      if (session === s) { publish({session: s.id, status: 'ended'}); stop(); }
    };
    s.watchdog = setInterval(() => {
      if (Date.now() - s.lastState > 2500) {
        publish({session: s.id, status: 'ended', error: 'Reference page stopped responding'});
        stop();
      }
    }, 500);
    s.lastState = Date.now();
    return {ok: true, sample_rate: s.context.sampleRate, context_state: s.context.state};
  } catch (error) {
    if (session === s) await stop();
    return {ok: false, error: error.message};
  }
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.target !== 'offscreen') return;
  if (message.type === 'start') { start(message).then(reply); return true; }
  if (message.type === 'stop') { stop().then(() => reply({ok: true})); return true; }
  if (message.type === 'state' && session?.sampler && message.session === session.id) {
    const s = session;
    s.sampler.update(message.state, s.context.currentTime);
    s.lastState = Date.now();
    s.token = s.sampler.state ? `${s.sampler.state.source}/${s.sampler.state.epoch}` : null;
    s.node.port.postMessage({token: s.token});
    reply({ok: true});
    return;
  }
  reply({ok: false});
});
