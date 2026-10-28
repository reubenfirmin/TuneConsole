// Automatic tab-local capture and inference. This page receives metadata only.
export class AudioPreviewSession {
  constructor({playback, changed, result, failure = () => {}, extensionIds = [], automatic = true,
    portName = 'tc-audio-preview'}) {
    Object.assign(this, {playback, changed, result, failure, extensionIds, automatic, portName});
    this.current = null;
    this.port = null;
    this.lastAttempt = null;
    this.connecting = new Set();
    if (extensionIds.length) queueMicrotask(() => this.connect());
  }

  validPlaying() {
    const observed = this.playback();
    const source = this.sourceClock;
    const playing = source?.clock_basis === 'ytm_song_clock' && observed
      && source.video_id === observed.video_id && source.at > observed.at
      ? {...observed, ...source} : observed;
    // This preview never adopts metadata. Unverified title/artist context must
    // not prevent analysis of the actual playing media; preserve that uncertainty
    // in the export instead of borrowing the production metadata trust gate.
    if (!playing?.video_id || playing.paused !== false
        || playing.muted || playing.tab_muted || playing.volume === 0
        || !Number.isFinite(playing.position) || !Number.isFinite(playing.at)
        || Date.now() - playing.at > 6000 || Date.now() < playing.at - 100) return null;
    return {...playing, position: playing.position + Math.max(0, Date.now() - playing.at) / 1000};
  }

  connect() {
    if (this.destroyed || this.port || this.connecting.size) return;
    clearTimeout(this.reconnect);
    if (Date.now() < this.retryConnectionAt) {
      this.reconnect = setTimeout(() => this.connect(), this.retryConnectionAt - Date.now());
      return;
    }
    if (!globalThis.chrome?.runtime?.connect) {
      this.changed({phase: 'idle', message: 'Reload the TuneConsole extension, then reload this page in Chrome or Chromium.'});
      this.reconnect = setTimeout(() => this.connect(), 2000);
      return;
    }
    for (const id of this.extensionIds) {
      try {
        const port = chrome.runtime.connect(id, {name: this.portName});
        this.connecting.add(port);
        const timeout = setTimeout(() => { if (this.connecting.delete(port)) port.disconnect(); }, 3000);
        port.onMessage.addListener(data => {
          if (data?.type === 'ready' && !this.port && !this.destroyed) {
            clearTimeout(timeout);
            this.connecting.delete(port);
            this.port = port;
            for (const other of this.connecting) other.disconnect();
            this.connecting.clear();
            this.changed({phase: 'idle', message: 'Ready. Analysis starts automatically when music is playing.'});
            this.tryAutomatic();
          } else if (data?.type === 'unavailable' && this.connecting.has(port)) {
            // No automatic recording when this extension build has no model.
            // Keep the retry quiet and infrequent; an extension update may add it.
            this.retryConnectionAt = Date.now() + 60000;
            clearTimeout(timeout);
            port.disconnect();
          } else if (this.port === port) this.receive(data);
        });
        port.onDisconnect.addListener(() => {
          // Consume Chrome's missing-extension error; the other build may exist.
          void chrome.runtime.lastError;
          clearTimeout(timeout);
          this.connecting.delete(port);
          if (this.port === port) {
            this.port = null;
            this.fail('Audio preview disconnected. Waiting for the TuneConsole extension…');
          }
          if (!this.port && !this.destroyed) this.reconnect = setTimeout(() => this.connect(), 2000);
        });
      } catch (e) {}
    }
    if (!this.port) {
      this.changed({phase: 'idle', message: 'Connecting to the TuneConsole extension…'});
      this.reconnect = setTimeout(() => this.connect(), 3500);
    }
  }

  check(s) {
    if (this.current !== s) return false;
    // HTTP status is only a scheduling hint. Delayed polls, buffering and clock
    // corrections cannot invalidate audio already being recorded in YouTube.
    // The source tab checks the media element, song, pause and seek directly.
    return true;
  }

  fail(message) {
    const s = this.current;
    this.stop(message);
    if (s) this.failure({phase: s.analysing ? 'analysing' : 'capturing', message: String(message).slice(0, 512)});
  }

  stop(message = 'Analysis stopped. Your music keeps playing.') {
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
    for (const port of this.connecting) port.disconnect();
    this.connecting.clear();
  }

  tryAutomatic() {
    if (!this.automatic) return;
    const playing = this.validPlaying();
    if (this.port && !this.current && playing && playing.video_id !== this.lastAttempt) this.start();
  }

  schedule(seconds) { this.start({notBefore: seconds}); }

  start({notBefore = null} = {}) {
    if (this.current || !this.port) return;
    const playing = this.validPlaying();
    if (!playing) return;
    this.lastAttempt = playing.video_id;
    const s = {playing, started: Date.now(), token: crypto.randomUUID()};
    this.current = s;
    const waiting = Number.isFinite(notBefore) && notBefore > playing.position;
    this.changed({phase: waiting ? 'waiting' : 'capturing', seconds: 0,
      message: waiting ? 'Waiting for the next audio check.' : 'Listening automatically… 0 of 10 seconds.'});
    s.timeout = setTimeout(() => {
      if (this.current === s) this.fail('Audio capture timed out. Check that YouTube Music is playing.');
    }, 17000 + (waiting ? (notBefore - playing.position) * 1000 : 0));
    try { this.port.postMessage({type: 'capture', id: s.token, video_id: playing.video_id,
      ...(Number.isFinite(notBefore) ? {not_before: notBefore} : {})}); }
    catch (error) { this.fail('Audio preview disconnected. Reload the TuneConsole extension.'); }
  }

  receive(data) {
    const s = this.current;
    if (!s || data?.id !== s.token || !this.check(s)) return;
    if (data.error) { this.fail(data.error); return; }
    if (data.type === 'waiting') return;
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
