// Preview tool's tab-local capture lifecycle; the app receives analysis metadata only.
export class AudioCaptureSession {
  constructor({playback, changed, result, failure = () => {}, extensionIds = []}) {
    Object.assign(this, {playback, changed, result, failure, extensionIds});
    this.current = null;
    this.port = null;
    this.lastAttempt = null;
    this.connecting = new Map();
    if (extensionIds.length) queueMicrotask(() => this.connect());
  }

  validPlaying() {
    const observed = this.playback();
    const source = this.sourceClock;
    const playing = source?.clock_basis === 'ytm_song_clock' && observed
      && source.video_id === observed.video_id && source.at > observed.at
      ? {...observed, ...source} : observed;
    // Capture never adopts metadata. Unverified title/artist context must
    // not prevent analysis of the actual playing media; preserve that uncertainty
    // in the export instead of borrowing the production metadata trust gate.
    if (!playing?.video_id || playing.paused !== false
        || playing.muted || playing.tab_muted || playing.volume === 0
        || !Number.isFinite(playing.position) || !Number.isFinite(playing.at)
        || Date.now() - playing.at > 6000 || Date.now() < playing.at - 100) return null;
    return {...playing, position: playing.position + Math.max(0, Date.now() - playing.at) / 1000};
  }

  connect() {
    if (this.destroyed || this.port) return;
    clearTimeout(this.reconnect);
    if (this.connecting.size) {
      this.reconnect = setTimeout(() => this.connect(), 2000);
      return;
    }
    if (Date.now() < this.retryConnectionAt) {
      this.reconnect = setTimeout(() => this.connect(), this.retryConnectionAt - Date.now());
      return;
    }
    if (!globalThis.chrome?.runtime?.connect) {
      this.changed({phase: 'idle',
        message: 'Reload the TuneConsole extension, then reload this page in Chrome or Chromium.'});
      this.reconnect = setTimeout(() => this.connect(), 2000);
      return;
    }
    for (const id of this.extensionIds) {
      try {
        const port = chrome.runtime.connect(id, {name: 'tc-audio-preview'});
        this.connecting.set(port, setTimeout(() => this.disconnectCandidate(port), 3000));
        port.onMessage.addListener(data => {
          if (this.destroyed || (!this.connecting.has(port) && this.port !== port)) return;
          if (data?.type === 'ready' && !this.port && !this.destroyed) {
            clearTimeout(this.reconnect);
            clearTimeout(this.connecting.get(port));
            this.connecting.delete(port);
            this.port = port;
            for (const other of this.connecting.keys()) this.disconnectCandidate(other);
            this.changed({phase: 'idle', message: 'Ready. Analysis starts automatically when music is playing.'});
            this.tryAutomatic();
          } else if (data?.type === 'unavailable' && this.connecting.has(port)) {
            // No automatic recording when this extension build has no model.
            // Keep the retry quiet and infrequent; an extension update may add it.
            this.retryConnectionAt = Date.now() + 60000;
            this.disconnectCandidate(port);
          } else if (this.port === port) this.receive(data);
        });
        port.onDisconnect.addListener(() => {
          // Consume Chrome's missing-extension error; the other build may exist.
          void chrome.runtime.lastError;
          clearTimeout(this.connecting.get(port));
          this.connecting.delete(port);
          if (this.port === port) {
            this.port = null;
            this.fail('Audio capture disconnected. Waiting for the TuneConsole extension…');
          }
          if (!this.port && !this.destroyed) {
            clearTimeout(this.reconnect);
            this.reconnect = setTimeout(() => this.connect(), 2000);
          }
        });
      } catch (e) {}
    }
    if (!this.port) {
      this.changed({phase: 'connecting', message: 'Connecting to the TuneConsole extension…'});
      clearTimeout(this.reconnect);
      this.reconnect = setTimeout(() => this.connect(), 3500);
    }
  }

  disconnectCandidate(port) {
    clearTimeout(this.connecting.get(port));
    this.connecting.delete(port);
    // Local Port.disconnect() notifies the peer, so settle our pending attempt here.
    try { port.disconnect(); } catch (error) {}
  }

  fail(message) {
    const s = this.current;
    this.stop(message);
    if (s) this.failure({phase: s.analysing ? 'analysing' : 'capturing', message: String(message).slice(0, 512)});
  }

  stop(message = 'Analysis stopped. Your music keeps playing.') {
    clearTimeout(this.retryCapture);
    const s = this.current;
    this.current = null;
    if (s) {
      clearTimeout(s.timeout);
      try { this.port?.postMessage({type: 'stop'}); } catch (e) {}
    }
    this.changed({phase: 'idle', message});
  }

  destroy() {
    this.destroyed = true;
    this.stop();
    clearTimeout(this.reconnect);
    this.port?.disconnect();
    this.port = null;
    for (const port of this.connecting.keys()) this.disconnectCandidate(port);
  }

  tryAutomatic() {
    const playing = this.validPlaying();
    if (this.port && !this.current && playing && playing.video_id !== this.lastAttempt) this.start();
  }

  start() {
    if (this.current || !this.port) return;
    const playing = this.validPlaying();
    if (!playing) return;
    this.lastAttempt = playing.video_id;
    const s = {playing, token: crypto.randomUUID()};
    this.current = s;
    this.changed({phase: 'capturing', seconds: 0, message: 'Listening automatically… 0 of 10 seconds.'});
    s.timeout = setTimeout(() => {
      if (this.current === s) this.fail('Audio capture timed out. Check that YouTube Music is playing.');
    }, 17000);
    try { this.port.postMessage({type: 'capture', id: s.token, video_id: playing.video_id}); }
    catch (error) { this.fail('Audio capture disconnected. Reload the TuneConsole extension.'); }
  }

  receive(data) {
    const s = this.current;
    if (!s || data?.id !== s.token) return;
    // The source tab validates song/pause/seek; delayed HTTP status cannot
    // invalidate an excerpt already being recorded or analysed there.
    if (data.error) { this.fail(data.error); return; }
    if (data.type === 'waiting') {
      if (data.collection_finishing) {
        this.stop('Finishing the captured audio check before starting preview…');
        this.lastAttempt = null;
        this.retryCapture = setTimeout(() => this.tryAutomatic(), 500);
      }
      return;
    }
    if (data.type === 'progress' && !s.analysing) {
      const seconds = Number.isFinite(data.seconds) ? Math.max(0, Math.min(10, data.seconds)) : 0;
      this.changed({phase: 'capturing', seconds, message: `Listening automatically… ${seconds.toFixed(1)} of 10 seconds.`});
    } else if (data.type === 'analysing' && !s.analysing) {
      s.analysing = true;
      clearTimeout(s.timeout);
      this.changed({phase: 'analysing', seconds: 10, message: 'Excerpt captured. Analysing inside the YouTube Music tab…'});
      s.timeout = setTimeout(() => {
        if (this.current === s) this.fail('Analysis exceeded the 35-second preview limit.');
      }, 35000);
    } else if (data.type === 'result') {
      if (data.result?.analysis_location !== 'youtube_tab' || data.result.audio_transferred_to_app !== false
          || !data.result.prediction?.moods?.traits) { this.fail('Invalid tab-local analysis result.'); return; }
      const source = data.result.source_playback;
      if (source?.clock_basis === 'ytm_song_clock' && source.video_id === s.playing.video_id
          && Number.isFinite(source.position)
          && Number.isFinite(source.at)) this.sourceClock = source;
      const result = {...data.result, schema: 3, status: 'unverified_playback_preview',
          observed_playback: {video_id: s.playing.video_id, title: s.playing.title, artist: s.playing.artist,
            metadata_verified: s.playing.metadata_verified === true},
          source_identity_verified: false, recording_association: null, recommendation_adoption: false,
          capture_seconds: 10, sample_rate: 16000,
          observed_position_at_start: s.playing.position,
          interval_basis: 'unverified_player_clock_estimate',
          raw_audio_retained: false};
      this.stop('Preview ready. Your music keeps playing.');
      this.result(result);
    } else if (data.type === 'pcm') this.fail('Only analysis metadata may leave the music tab.');
  }
}
