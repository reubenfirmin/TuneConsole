// Capture and inference both stay in the YTM tab. Runtime ports carry metadata.
(() => {
  if (location.origin !== 'https://music.youtube.com') return;
  const REVISION = 3;
  if (window.__tcAudioPreviewCapture?.revision >= REVISION) return;
  window.__tcAudioPreviewCapture?.stop();
  let current = null, waiting = null;
  function clearWaiting() {
    if (waiting) clearInterval(waiting.poll);
    waiting = null;
  }
  let playerIdentity = null;
  const readyMedia = () => [...document.querySelectorAll('video,audio')]
    .find(item => !item.paused && !item.ended && item.readyState >= 2);
  function unavailableMedia() {
    const elements = [...document.querySelectorAll('video,audio')];
    const states = elements.slice(0, 4).map(item =>
      `paused=${+item.paused},ended=${+item.ended},ready=${item.readyState},` +
      `network=${item.networkState},connected=${+item.isConnected},error=${item.error?.code || 0}`);
    return ('Audio capture found no ready playing media in YouTube Music ' +
      `[music_media_unavailable; elements=${elements.length}; ${states.join('; ')}]`).slice(0, 500);
  }
  function playerStatus(event) {
    // Use the existing player-derived sensor, never the browsed page URL:
    // listeners may browse another album while the current song keeps playing.
    const data = event.data;
    if (event.source !== window || data?.__tcSensorVersion !== 4
        || !Number.isSafeInteger(data.__tcSensorRevision) || data.__tcSensorRevision < 8
        || !data.__tcNow) return;
    const media = readyMedia();
    playerIdentity = {videoId: data.__tcNow.videoId, position: data.__tcNow.position,
      media, mediaPosition: media?.currentTime, source: media?.currentSrc,
      sourceObject: media?.srcObject, at: Date.now()};
  }
  window.addEventListener('message', playerStatus);
  const differentSong = expected => expected && playerIdentity?.videoId
    && Date.now() - playerIdentity.at < 8000 && playerIdentity.videoId !== expected;
  function songPosition(media, expected) {
    const anchor = playerIdentity;
    if (!anchor || anchor.videoId !== expected || anchor.media !== media
        || Date.now() - anchor.at > 8000 || Date.now() < anchor.at
        || !Number.isFinite(anchor.position) || anchor.position < 0
        || media.currentSrc !== anchor.source || media.srcObject !== anchor.sourceObject) return null;
    // YTM can keep one media timeline across many songs. Anchor its progress
    // to MAIN's per-song player clock; never compare raw currentTime to targets.
    const delta = media.currentTime - anchor.mediaPosition;
    const elapsed = (Date.now() - anchor.at) / 1000;
    if (!Number.isFinite(delta) || delta < 0
        || delta > elapsed * (media.playbackRate || 1) + 1.5) return null;
    return anchor.position + delta;
  }
  const MIME = 'audio/webm;codecs=pcm', MAX_BYTES = 16 * 1024 * 1024;
  const erase = pcm => {
    if (pcm) for (let channel = 0; channel < pcm.numberOfChannels; channel++) pcm.getChannelData(channel).fill(0);
  };
  function guard(stream) {
    const state = {stopped: false};
    stream.addEventListener('addtrack', ({track}) => { if (state.stopped) track.stop(); });
    return state;
  }
  function release(run) {
    if (run.guard) run.guard.stopped = true;
    run.stream?.getTracks().forEach(track => track.stop());
  }
  function clean(run) {
    if (current === run) current = null;
    clearInterval(run.poll);
    clearTimeout(run.timeout);
    for (const [name, listener] of run.listeners) run.media.removeEventListener(name, listener);
    run.recorder.ondataavailable = run.recorder.onstop = run.recorder.onerror = null;
    if (run.recorder.state !== 'inactive') run.recorder.stop();
    release(run);
    run.channel?.close();
    run.frame?.remove();
    run.chunks.length = 0;
    if (run.encoded?.byteLength) run.encoded.fill(0);
    erase(run.pcm);
    if (run.samples?.byteLength) run.samples.fill(0);
    for (const channel of run.measurements || []) if (channel.byteLength) channel.fill(0);
  }
  function fail(run, error) {
    if (current !== run) return;
    try { run.port.postMessage({id: run.id, error}); } catch (e) {}
    clean(run);
  }
  async function decode(run) {
    if (current !== run) return;
    try {
      const encoded = await new Blob(run.chunks, {type: MIME}).arrayBuffer();
      run.chunks.length = 0;
      if (current !== run) { new Uint8Array(encoded).fill(0); return; }
      run.encoded = new Uint8Array(encoded);
      // decodeAudioData resamples to this offline context's rate; no live output.
      const pcm = await new OfflineAudioContext(2, 1, 44100).decodeAudioData(encoded);
      if (current !== run) { erase(pcm); return; }
      run.pcm = pcm;
      if (pcm.length < 441000 || pcm.numberOfChannels > 2)
        throw Error(`The audio excerpt was interrupted or too short (${pcm.duration.toFixed(3)} seconds available).`);
      // Preserve the full-band mono/stereo input for LUFS and beat tracking.
      // Only the model input is downmixed and resampled to 16 kHz.
      const measurements = run.measurements = Array.from({length: pcm.numberOfChannels},
        (_, channel) => pcm.getChannelData(channel).slice(0, 441000));
      const context = new OfflineAudioContext(1, 160000, 16000);
      const source = context.createBufferSource();
      source.buffer = pcm;
      source.connect(context.destination);
      source.start();
      const rendered = await context.startRendering();
      if (current !== run) { erase(rendered); return; }
      const samples = run.samples = rendered.getChannelData(0).slice();
      erase(rendered);
      let peak = 0;
      for (const sample of samples) {
        if (!Number.isFinite(sample)) throw Error('Invalid audio samples.');
        peak = Math.max(peak, Math.abs(sample));
      }
      if (peak <= .000001) throw Error('No usable audio signal arrived. Silence or a capture restriction may be responsible.');
      // No PCM serialization or runtime messaging. The extension frame and its
      // dedicated worker belong to this same browser tab.
      erase(run.pcm);
      clearTimeout(run.timeout);
      run.timeout = setTimeout(() => fail(run, 'Tab-local analysis timed out.'), 32000);
      run.port.postMessage({id: run.id, type: 'analysing'});
      const frame = run.frame = document.createElement('iframe');
      const frameURL = chrome.runtime.getURL('audio_preview_frame.html');
      const frameOrigin = new URL(frameURL).origin;
      frame.hidden = true;
      frame.src = frameURL;
      frame.title = 'TuneConsole local audio analysis';
      frame.onload = () => {
        if (current !== run) return;
        if (frame.src !== frameURL || !frame.isConnected) { fail(run, 'The local analysis frame changed.'); return; }
        const channel = new MessageChannel();
        run.channel = channel.port1;
        channel.port1.onmessage = ({data}) => {
          if (current !== run || data?.id !== run.id) return;
          if (data.error) { fail(run, data.error); return; }
          if (data.type === 'ready') {
            channel.port1.postMessage({id: run.id, samples, rate: 16000,
              measurement_left: measurements[0], measurement_right: measurements[1] || null,
              measurement_rate: 44100}, [samples.buffer, ...measurements.map(input => input.buffer)]);
          } else if (data.prediction) {
            const position = songPosition(run.media, run.videoId);
            run.port.postMessage({id: run.id, type: 'result', result: {
              ...data, analysis_location: 'youtube_tab', audio_transferred_to_app: false,
              capture_method: 'HTMLMediaElement.captureStream',
              pcm_reader: 'MediaRecorder PCM / OfflineAudioContext.decodeAudioData',
              realtime_audio_context_used: false, trigger: 'automatic',
              user_activation_at_start: run.activation, capture_sample_rate: run.inputRate,
              capture_seconds: 10, recorded_seconds: run.recordedSeconds,
              source_position_at_start: run.position,
              song_position_at_start: run.songPosition,
              source_playback: position === null ? null : {
                video_id: run.videoId, position, clock_basis: 'ytm_song_clock',
                paused: Boolean(run.media.paused || run.media.ended || differentSong(run.videoId)),
                muted: run.media.muted, volume: run.media.volume, at: Date.now()},
              source_identity_verified: false, raw_audio_retained: false,
            }});
            clean(run);
          }
        };
        frame.contentWindow.postMessage({type: 'tc-audio-preview-init', id: run.id},
          frameOrigin, [channel.port2]);
      };
      document.documentElement.append(frame);
    } catch (error) { fail(run, error.message || 'Offline audio decoding failed.'); }
  }
  function connect(port) {
    if (port.name !== 'tc-audio-preview-capture' || port.sender?.id !== chrome.runtime.id) return;
    port.onDisconnect.addListener(() => {
      if (waiting?.port === port) clearWaiting();
      if (current?.port === port) clean(current);
    });
    function begin(message, mediaDeadline = null) {
      if (message?.type !== 'capture' || typeof message.id !== 'string') return;
      if (current || waiting) { port.postMessage({id: message.id, error: 'Audio capture is already running.'}); return; }
      let run;
      try {
        if (differentSong(message.video_id))
          throw Error('The player moved to another song. Waiting for its playback update.');
        const media = readyMedia();
        if (!media) {
          // Buffering and DOM replacement can outlast the scheduling snapshot.
          // Recover here rather than depending on the background app's timers.
          const deadline = mediaDeadline ?? Date.now() + 3000;
          if (Date.now() >= deadline) throw Error(unavailableMedia());
          const pending = waiting = {port};
          port.postMessage({id: message.id, type: 'waiting'});
          pending.poll = setInterval(() => {
            if (waiting !== pending) return;
            if (differentSong(message.video_id) || readyMedia() || Date.now() >= deadline) {
              clearWaiting();
              begin(message, deadline);
            }
          }, 250);
          return;
        }
        const position = songPosition(media, message.video_id);
        if (Number.isFinite(message.not_before) && message.not_before >= 0 && message.not_before <= 86400
            && (position === null || position < message.not_before)) {
          const pending = waiting = {port, media, source: media.currentSrc, sourceObject: media.srcObject};
          port.postMessage({id: message.id, type: 'waiting'});
          pending.poll = setInterval(() => {
            if (waiting !== pending) return;
            if (differentSong(message.video_id) || !media.isConnected || media.paused || media.ended
                || media.currentSrc !== pending.source || media.srcObject !== pending.sourceObject) {
              clearWaiting();
              port.postMessage({id: message.id, error: 'Playback changed before the next audio check.'});
            } else if ((songPosition(media, message.video_id) ?? -1) >= message.not_before) {
              clearWaiting(); begin({...message, not_before: null});
            }
          }, 250);
          return;
        }
        if (media.mediaKeys) throw Error('This player uses DRM. Direct audio capture is blocked.');
        if (!media.captureStream || typeof MediaRecorder !== 'function' || !MediaRecorder.isTypeSupported(MIME))
          throw Error('This browser does not support lossless direct audio capture.');
        // Chromium's element capture routes interfere when the old three-second
        // diagnostic and the model excerpt capture the same player concurrently.
        // Retire the diagnostic before opening the model's single capture route.
        window.__tcAudioProbe?.stop();
        const stream = media.captureStream(), trackGuard = guard(stream);
        const audio = stream.getAudioTracks();
        stream.getVideoTracks().forEach(track => track.stop());
        if (!audio.length) { trackGuard.stopped = true; stream.getTracks().forEach(track => track.stop());
          throw Error('The player did not expose an audio track.'); }
        run = {id: message.id, videoId: message.video_id, port, media, stream, guard: trackGuard, chunks: [], bytes: 0, listeners: [],
          source: media.currentSrc, sourceObject: media.srcObject, started: performance.now(),
          position: media.currentTime, songPosition: position,
          inputRate: audio[0].getSettings().sampleRate || null,
          activation: {is_active: navigator.userActivation?.isActive ?? null,
            has_been_active: navigator.userActivation?.hasBeenActive ?? null}};
        // Set the run before constructing the recorder so every failure releases tracks.
        current = run;
        run.recorder = new MediaRecorder(new MediaStream(audio), {mimeType: MIME});
        run.recorder.ondataavailable = ({data}) => {
          if (current !== run || !data.size) return;
          run.bytes += data.size;
          if (run.bytes > MAX_BYTES) { fail(run, 'The audio excerpt exceeded its memory limit.'); return; }
          run.chunks.push(data);
        };
        run.recorder.onerror = () => fail(run, 'Direct audio recording failed.');
        run.recorder.onstop = () => { void decode(run); };
        for (const event of ['pause', 'ended', 'seeking', 'emptied', 'encrypted']) {
          const reasons = {pause: 'Playback paused before the excerpt finished.',
            ended: 'The song ended before the excerpt finished.',
            seeking: 'Playback was moved during the excerpt.',
            emptied: 'The player replaced its audio source.', encrypted: 'The player enabled protected audio.'};
          const listener = () => fail(run, reasons[event]);
          media.addEventListener(event, listener);
          run.listeners.push([event, listener]);
        }
        run.recorder.start(100);
        port.postMessage({id: run.id, type: 'progress', seconds: 0});
        run.poll = setInterval(() => {
          if (differentSong(message.video_id)) {
            fail(run, 'The song changed before the excerpt finished.'); return;
          }
          if (media.mediaKeys || !media.isConnected || media.paused || media.ended || media.seeking ||
              media.currentSrc !== run.source || media.srcObject !== run.sourceObject ||
              (!run.recordedSeconds && audio.some(track => track.readyState === 'ended'))) {
            fail(run, 'Playback changed during audio capture.'); return;
          }
          const seconds = (performance.now() - run.started) / 1000;
          if (run.recorder.state === 'inactive') return;
          port.postMessage({id: run.id, type: 'progress', seconds: Math.min(10, seconds)});
          // Recorder frames need a small flush margin. Select exactly ten
          // seconds after decoding, never pad missing audio with synthetic zeros.
          if (seconds >= 10.25) {
            run.recordedSeconds = seconds;
            // The excerpt is complete. Playback may pause or move on while its
            // immutable recording is decoded and analysed in this tab.
            clearInterval(run.poll);
            for (const [name, listener] of run.listeners) media.removeEventListener(name, listener);
            run.listeners.length = 0;
            run.recorder.stop(); release(run);
          }
        }, 100);
        run.timeout = setTimeout(() => fail(run, 'Audio capture or decoding timed out.'), 15000);
      } catch (error) {
        if (run) {
          // A recorder constructor can fail before run.recorder exists.
          if (!run.recorder) run.recorder = {state: 'inactive'};
          fail(run, error.name === 'SecurityError' ? 'The player blocks cross-origin audio capture.' : error.message);
        } else port.postMessage({id: message.id, error: error.message || 'Direct audio capture failed.'});
      }
    }
    port.onMessage.addListener(message => begin(message));
  }
  const controller = window.__tcAudioPreviewCapture = {revision: REVISION,
    inspect() { return {running: !!(current || waiting)}; }, stop() {
    clearWaiting();
    if (current) clean(current);
    chrome.runtime.onConnect.removeListener(connect);
    window.removeEventListener('message', playerStatus);
    window.removeEventListener('pagehide', controller.stop);
    clearInterval(lifetime);
    delete window.__tcAudioPreviewCapture;
  }};
  chrome.runtime.onConnect.addListener(connect);
  window.addEventListener('pagehide', controller.stop);
  const lifetime = setInterval(() => {
    try { if (!chrome.runtime.id) controller.stop(); } catch (e) { controller.stop(); }
  }, 1000);
})();
