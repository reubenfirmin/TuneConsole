// Automatic one-shot check on Music's existing player. No output or capture picker.
(() => {
  if (location.origin !== "https://music.youtube.com") return;
  if (window.__tcAudioPreviewCapture?.inspect().running) return;
  const previous = window.__tcAudioProbe;
  if (previous?.revision >= 2 && !previous.stopped) return;
  previous?.stop();
  const DISMISSED = "tuneconsole-audio-probe-auto-dismissed";
  try { if (sessionStorage.getItem(DISMISSED) === "1") return; } catch (e) {}

  let current = null, report = null, automaticPending = true;
  const MIME = "audio/webm;codecs=pcm", MAX_BYTES = 8 * 1024 * 1024;
  // A reload can invalidate an old content world before its cleanup timer runs.
  document.getElementById("tuneconsole-audio-probe")?.remove();
  const host = document.createElement("aside");
  host.id = "tuneconsole-audio-probe";
  const root = host.attachShadow({mode: "open"});
  root.innerHTML = `<style>
    :host { position: fixed; bottom: 104px; right: 16px; width: min(420px, calc(100vw - 32px));
      z-index: 2501; color-scheme: dark; font: 14px/1.5 system-ui, sans-serif; color: #f6f8fa; }
    section { position: relative; padding: 18px; border: 1px solid #50665c; border-radius: 8px;
      background: #16221c; box-shadow: 0 8px 28px #0006; }
    h2 { margin: 0 28px 8px 0; font: 600 17px/1.3 system-ui, sans-serif; }
    p { margin: 8px 0; } .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    button { border: 1px solid #81968a; border-radius: 4px; padding: 7px 12px;
      background: transparent; color: inherit; font: 600 14px system-ui; cursor: pointer; }
    [data-start] { color: #10231a; background: #8be0b3; border-color: #8be0b3; }
    button:disabled { opacity: .55; cursor: default; }
    [data-dismiss] { position: absolute; right: 7px; top: 7px; border: 0; padding: 0;
      width: 32px; height: 32px; font-size: 22px; }
    button:focus-visible, summary:focus-visible { outline: 3px solid #fff; outline-offset: 3px; }
    details { margin-top: 12px; } summary { cursor: pointer; } small { color: #c6d2cc; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 180px; overflow: auto;
      font: 12px/1.5 ui-monospace, monospace; }
    [hidden] { display: none !important; }
  </style>
  <section aria-labelledby="tc-audio-probe-title">
    <button type="button" data-dismiss aria-label="Dismiss audio access check">×</button>
    <h2 id="tc-audio-probe-title">TuneConsole audio access check</h2>
    <p>Automatically checks three seconds of the music already playing.
      No capture button or sharing dialog is needed.</p>
    <p role="status" data-status>Waiting for music. Audio access will be checked automatically.</p>
    <div class="actions">
      <button type="button" data-start>Run check again</button>
      <button type="button" data-stop disabled>Stop check</button>
      <button type="button" data-copy hidden>Copy report</button>
    </div>
    <details hidden><summary>Audio access report</summary><pre></pre></details>
    <p><small>This checks audio access only. It does not predict tags or save audio.</small></p>
  </section>`;
  const find = name => root.querySelector(`[data-${name}]`);
  const startButton = find("start"), stopButton = find("stop"), status = find("status");
  const details = root.querySelector("details"), output = root.querySelector("pre");

  function render(message, value = null) {
    status.textContent = message;
    startButton.disabled = current !== null;
    stopButton.disabled = current === null;
    report = value;
    find("copy").hidden = !report;
    find("copy").textContent = "Copy report";
    details.hidden = !report;
    output.textContent = report ? JSON.stringify(report, null, 2) : "";
  }

  function base(media) {
    return {schema: "tuneconsole-direct-audio-probe-2", capture_method: "HTMLMediaElement.captureStream",
      pcm_reader: "MediaRecorder PCM / OfflineAudioContext.decodeAudioData",
      realtime_audio_context_used: false,
      drm_media_keys: media ? media.mediaKeys != null : null,
      // Never copy the signed media URL, cookies or request headers into diagnostics.
      source_kind: !media ? null : media.srcObject ? "stream" : media.currentSrc.startsWith("blob:")
        ? "blob" : media.currentSrc.startsWith("http") ? "http" : "other",
      source_identity_verified: false, raw_audio_retained: false};
  }

  function guardTracks(stream) {
    // This closure deliberately lives outside start(), so it cannot retain a
    // completed run's PCM, player reference or analysis callbacks.
    const guard = {stopped: false};
    stream.addEventListener("addtrack", ({track}) => {
      if (guard.stopped) track.stop();
    });
    return guard;
  }

  function releaseTracks(run) {
    if (run.trackGuard) run.trackGuard.stopped = true;
    // Only stop this capture's tracks, never the source player's srcObject tracks.
    for (const track of run.stream?.getTracks() || []) track.stop();
  }

  function erasePCM(buffer) {
    if (buffer) for (let channel = 0; channel < buffer.numberOfChannels; channel++)
      buffer.getChannelData(channel).fill(0);
  }

  function cleanup(run) {
    clearInterval(run.poll);
    clearTimeout(run.deadline);
    for (const [event, listener] of run.listeners) run.media.removeEventListener(event, listener);
    for (const [track, listener] of run.trackListeners) track.removeEventListener("ended", listener);
    if (run.recorder) {
      run.recorder.ondataavailable = run.recorder.onstop = run.recorder.onerror = null;
      if (run.recorder.state !== "inactive") run.recorder.stop();
    }
    releaseTracks(run);
    run.chunks.length = 0;
    // decodeAudioData can detach its input buffer; a detached view has zero length.
    if (run.encoded?.byteLength) run.encoded.fill(0);
    erasePCM(run.pcm);
  }

  function finish(run, outcome, message) {
    if (current !== run) return;
    current = null;
    const rms = run.count ? Math.sqrt(run.squares / run.count) : null;
    const result = {...run.base, outcome,
      audio_tracks: run.stream?.getAudioTracks().length || 0,
      capture_tracks_muted: (run.stream?.getAudioTracks() || []).map(track => track.muted),
      recording_mime: run.recorder?.mimeType || null, recorded_bytes: run.bytes,
      sample_rate: run.pcm?.sampleRate || null, channels: run.pcm?.numberOfChannels || null,
      decoded_seconds: run.pcm?.duration || null,
      measured_seconds: run.capturedSeconds ?? (run.started === null ? 0 : (performance.now() - run.started) / 1000),
      recording_chunks: run.chunkCount, samples_inspected: run.count, rms, peak: run.peak,
      rms_dbfs: rms > 0 ? 20 * Math.log10(rms) : null,
      playback_continued: !run.media.paused && !run.media.ended,
      media_time_advanced: run.media.currentTime - run.mediaTime};
    cleanup(run);
    if (!controller.stopped) render(message, result);
  }

  function playingMedia() {
    return [...document.querySelectorAll("video, audio")]
      .find(item => !item.paused && !item.ended && item.readyState >= 2);
  }

  async function decode(run) {
    if (current !== run || run.phase !== "decoding") return;
    try {
      const blob = new Blob(run.chunks, {type: MIME});
      run.chunks.length = 0;
      const encoded = await blob.arrayBuffer();
      if (current !== run) { new Uint8Array(encoded).fill(0); return; }
      run.encoded = new Uint8Array(encoded);
      // Offline decoding neither opens audio output nor needs user activation.
      const offline = new OfflineAudioContext(1, 1, run.sampleRate);
      const pcm = await offline.decodeAudioData(encoded);
      if (current !== run) { erasePCM(pcm); return; }
      run.pcm = pcm;
      for (let channel = 0; channel < pcm.numberOfChannels; channel++) {
        for (const sample of pcm.getChannelData(channel)) {
          run.squares += sample * sample;
          run.peak = Math.max(run.peak, Math.abs(sample));
          run.count++;
        }
      }
      const usable = run.peak > 0.000001 && run.media.currentTime > run.mediaTime;
      finish(run, usable ? "available" : "no_signal", usable
        ? run.base.trigger === "automatic"
          ? "Direct audio access works automatically. No capture click or sharing dialog was needed."
          : "Direct audio access works. The retry needed no sharing dialog."
        : "No usable signal arrived. This could be silence or a capture restriction.");
    } catch (error) {
      finish(run, "decode_failed", `Offline audio decoding failed (${error.name || "unknown error"}).`);
    }
  }

  function start(trigger = "manual_retry") {
    if (current || controller.stopped) return;
    const media = playingMedia();
    if (!media) {
      render("Play a song in YouTube Music, then try again.", {...base(null), outcome: "no_playing_media"});
      return;
    }
    automaticPending = false;
    const info = {...base(media), trigger, user_activation_at_start: {
      is_active: navigator.userActivation?.isActive ?? null,
      has_been_active: navigator.userActivation?.hasBeenActive ?? null}};
    if (info.drm_media_keys) {
      render("This player uses DRM. Direct audio capture is blocked.", {...info, outcome: "drm_blocked"});
      return;
    }
    if (typeof media.captureStream !== "function") {
      render("This browser does not support direct player capture.", {...info, outcome: "unsupported"});
      return;
    }
    if (typeof MediaRecorder !== "function" || !MediaRecorder.isTypeSupported(MIME)) {
      render("This browser does not support the lossless PCM audio check.", {...info, outcome: "unsupported_pcm"});
      return;
    }
    const run = {media, base: info, source: media.currentSrc, sourceObject: media.srcObject,
      mediaTime: media.currentTime, started: null, stream: null, recorder: null, pcm: null,
      phase: "capturing", chunks: [], bytes: 0, chunkCount: 0,
      listeners: [], trackListeners: [], squares: 0, count: 0, peak: 0};
    current = run;
    render("Checking three seconds of the playing audio…");
    run.deadline = setTimeout(() => finish(run, "timeout",
      "The audio check timed out. Keep this tab visible and try again."), 8000);
    try {
      run.stream = media.captureStream();
      // Chrome can add fresh tracks on a later loadedmetadata even after every
      // original track was stopped. Keep this tiny guard on the native stream so
      // those future capture tracks are immediately stopped too. It retains no
      // PCM, context, player reference or active analysis callback.
      run.trackGuard = guardTracks(run.stream);
      for (const track of run.stream.getVideoTracks()) track.stop();
      const audio = run.stream.getAudioTracks();
      if (!audio.length) {
        finish(run, "no_audio_track", "The player did not expose an audio track.");
        return;
      }
      for (const track of audio) {
        const ended = () => finish(run, "capture_ended", "Audio access ended. Play a song and try again.");
        track.addEventListener("ended", ended);
        run.trackListeners.push([track, ended]);
      }
      for (const event of ["pause", "ended", "seeking", "emptied", "encrypted"]) {
        const changed = () => finish(run, "playback_changed", "Playback changed. Play a song and try again.");
        media.addEventListener(event, changed);
        run.listeners.push([event, changed]);
      }
      const sampleRate = audio[0].getSettings().sampleRate;
      run.sampleRate = Number.isFinite(sampleRate) && sampleRate >= 8000 && sampleRate <= 192000
        ? sampleRate : 48000;
      run.recorder = new MediaRecorder(new MediaStream(audio), {mimeType: MIME});
      run.recorder.ondataavailable = ({data}) => {
        if (current !== run || !data.size) return;
        run.bytes += data.size;
        if (run.bytes > MAX_BYTES) {
          finish(run, "buffer_limit", "The audio check exceeded its memory limit and stopped.");
          return;
        }
        run.chunks.push(data);
        run.chunkCount++;
      };
      run.recorder.onerror = () => finish(run, "capture_failed", "Direct audio recording failed.");
      run.recorder.onstop = () => {
        if (run.phase === "decoding") void decode(run);
        else finish(run, "capture_ended", "Audio access ended. Play a song and try again.");
      };
      run.started = performance.now();
      run.recorder.start(100);
      run.poll = setInterval(() => {
        if (media.mediaKeys || !media.isConnected || media.paused || media.ended || media.seeking ||
            media.currentSrc !== run.source || media.srcObject !== run.sourceObject) {
          finish(run, "playback_changed", "Playback changed. Play a song and try again.");
          return;
        }
        if (performance.now() - run.started >= 3000) {
          clearInterval(run.poll);
          run.phase = "decoding";
          run.capturedSeconds = (performance.now() - run.started) / 1000;
          run.recorder.stop();
          releaseTracks(run);
        }
      }, 50);
    } catch (error) {
      const outcome = error.name === "SecurityError" ? "cross_origin_blocked" : "capture_failed";
      finish(run, outcome, outcome === "cross_origin_blocked"
        ? "This player's audio is blocked by cross-origin restrictions."
        : `Direct audio capture failed (${error.name || "unknown error"}).`);
    }
  }

  const controller = window.__tcAudioProbe = {revision: 2, stopped: false,
    inspect() { return report ? {...report} : null; },
    stop() {
      controller.stopped = true;
      automaticPending = false;
      if (current) {
        const run = current;
        current = null;
        cleanup(run);
      }
      clearInterval(lifetime);
      window.removeEventListener("pagehide", onPageHide);
      document.removeEventListener("playing", maybeStart, true);
      host.remove();
    }};
  const onPageHide = () => controller.stop();
  const maybeStart = () => {
    if (automaticPending && !controller.stopped && !current && playingMedia()) start("automatic");
  };
  window.addEventListener("pagehide", onPageHide);
  document.addEventListener("playing", maybeStart, true);
  const lifetime = setInterval(() => {
    try { if (!chrome.runtime.id) controller.stop(); } catch (e) { controller.stop(); }
    maybeStart();
  }, 1000);
  startButton.addEventListener("click", () => start());
  stopButton.addEventListener("click", () => {
    if (current) finish(current, "stopped", "Audio check stopped. Your music keeps playing.");
  });
  find("dismiss").addEventListener("click", () => {
    try { sessionStorage.setItem(DISMISSED, "1"); } catch (e) {}
    controller.stop();
  });
  find("copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(output.textContent);
      find("copy").textContent = "Copied";
    } catch (e) {
      details.open = true;
      status.textContent = "Copy the JSON from Audio access report below.";
    }
  });
  document.documentElement.append(host);
  maybeStart();
})();
