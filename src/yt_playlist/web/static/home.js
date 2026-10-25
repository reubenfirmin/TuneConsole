function onboardingArrival() {
  return {
    songs: 0, playlists: 0, albums: 0, covers: [], outcome: null, statusError: '',
    // Import progress belongs to this view, separate from Home's SSE attachment.
    running: false,
    imported: false, analyzed: 0, total: 0, genres: 0, model_tracks: 0, modes: 0, discoveries: 0,
    _timer: null, _disposed: false,
    applyArrival(data) {
      this.songs = data.songs; this.playlists = data.playlists; this.albums = data.albums;
      this.outcome = data.outcome;
      Object.assign(this, data.setup || {});
      // Keep arrived covers in place rather than reshuffling the graphic on each poll.
      this.covers = [...new Set([...this.covers, ...data.covers])].slice(0, 6);
      this.statusError = '';
      if (data.setup?.complete && (data.setup.offers_ready || data.setup.mixes_ready || data.setup.albums_ready)
          && !this._openingHome) {
        this._openingHome = true;
        // Replace only Home once complete offers exist. The shared player and setup
        // details stay mounted, and navigating elsewhere never brings the user back.
        htmx.ajax('GET', '/', {target: '#home', select: '#home', swap: 'outerHTML'})
          .then(() => document.querySelector('main.page-home')?.classList.remove('page-setup'))
          .catch(() => { this._openingHome = false; });
      }
    },
    async refreshArrival() {
      try {
        const response = await fetch('/home/onboard/status');
        if (!response.ok) throw new Error('Import status could not be loaded.');
        const data = await response.json();
        if (this._disposed) return;
        this.applyArrival(data);
      } catch (_) {
        if (!this._disposed) this.statusError = 'Reconnecting to import progress…';
      } finally {
        if (!this._disposed && !this.complete) this._timer = setTimeout(() => this.refreshArrival(), 3000);
      }
    },
    init() {
      if (window.setupStatus) {
        this._onSetup = event => this.applyArrival(event.detail);
        window.addEventListener('setup-status', this._onSetup);
        if (window.setupStatus.snapshot) this.applyArrival(window.setupStatus.snapshot);
      } else this.refreshArrival();
    },
    destroy() {
      this._disposed = true; clearTimeout(this._timer);
      window.removeEventListener('setup-status', this._onSetup);
    },
  };
}

function homeStatus() {
  // Home shares playback status and refreshes its offers after a successful background sync.
  // Retryable sync failures stay in diagnostics; account actions live in Needs attention.
  return {
    connected: false,
    nowPlaying: null,
    radioActive: false,
    radioReady: false,
    get radioPlaybackState() {
      const song = this.nowPlaying;
      if (!this.connected || !this.radioActive || !this.radioReady || !song?.video_id
          || !(song.title || song.artist)
          || song.source?.kind !== 'radio' || typeof song.paused !== 'boolean') return '';
      return song.paused ? 'paused' : 'playing';
    },
    sensorHealth: null,
    running: false,
    failed: false,
    _job: null,            // guards against attaching to the same job twice
    _scan: null, _stream: null, _disposed: false, _initialized: false,
    destroy() {
      this._disposed = true;
      clearInterval(this._scan);
      this._stream?.close();
    },
    applyBridgeStatus(data) {
      if (this._disposed) return;
      this.connected = !!data.connected;
      this.nowPlaying = data.now_playing ? {...data.now_playing} : null;
      this.radioActive = !!data.radio;
      this.radioReady = !!data.radio_ready;
      this.sensorHealth = data.sensor_health || null;
    },
    init() {
      if (this._initialized) return;
      this._initialized = true;
      this.$el.closest('main.page-home')?.classList.toggle('page-setup', !!this.$el.querySelector('#onboarding-feed'));
      window.playbackStatus.start();
      if (window.playbackStatus.snapshot) this.applyBridgeStatus(window.playbackStatus.snapshot);
      // Watch completion without surfacing routine background retries as user actions.
      const scan = () => { if (this._disposed || this.running) return; fetch('/sync/active').then(r => r.json())
        .then(d => { if (d && d.job_id) this.attach(d.job_id); }).catch(() => {}); };
      scan();
      this._scan = setInterval(scan, 3000);
    },
    attach(jobId) {
      if (this._disposed || this.running || this._job === jobId) return;
      this._job = jobId; this.running = true; this.failed = false;
      this._stream?.close();
      const es = this._stream = new EventSource(`/sync/events/${jobId}`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === 'err' || ev.type === 'auth_expired') this.failed = true;
        if (ev.type === 'end') {
          es.close(); this.running = false;
          if (ev.outcome === 'complete' && !ev.error && !this.failed) {
            // Setup advances through prepared offers, not import completion. Keep
            // its current screen, shared player and open Details mounted.
            if (!this.$el.querySelector('#onboarding-feed')) {
              htmx.ajax('GET', '/', {target: '#home', select: '#home', swap: 'outerHTML'})
                .catch(() => {});
            }
          } else {
            this.failed = true;
          }
          return;
        }
      };
      es.onerror = () => { es.close(); this.running = false; this.failed = true; };
    },
  };
}
