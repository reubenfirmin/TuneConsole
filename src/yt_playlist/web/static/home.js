function onboardingArrival() {
  return {
    songs: 0, playlists: 0, albums: 0, covers: [], outcome: null, statusError: '',
    // Import progress belongs to this view, separate from Home's SSE attachment.
    running: false,
    imported: false, analyzed: 0, total: 0, genres: 0, model_tracks: 0, modes: 0, discoveries: 0,
    listening_format: 'mixes', activity: '', current: null, target: null, starterReady: false,
    _timer: null, _disposed: false,
    get progressLabel() {
      if (this.statusError) return this.statusError;
      if (!this.connected && !this.running) return 'Open YouTube Music to continue setup';
      if (this.running && this.current && this.target && ['playlists', 'albums'].includes(this.activity)) {
        return 'Reading ' + (this.activity === 'albums' ? 'album ' : 'playlist ')
          + this.current + ' of ' + this.target;
      }
      if (!this.imported && !this.songs) return 'Bringing in your music';
      if (this.listening_format === 'albums') return 'Finding your first album';
      if (this.listening_format === 'radio') return 'Preparing your first Radio Session';
      return 'Finding your first mix tape';
    },
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
      // The starter arrives independently through HTMX, often before sync finishes.
      this._onStarter = () => { this.starterReady = !!this.$el.querySelector('.starter-mix, [data-onboarding-starter]'); };
      this.$el.addEventListener('htmx:afterSwap', this._onStarter);
      this._onStarter();
      if (window.setupStatus) {
        this._onSetup = event => this.applyArrival(event.detail);
        window.addEventListener('setup-status', this._onSetup);
        if (window.setupStatus.snapshot) this.applyArrival(window.setupStatus.snapshot);
      } else this.refreshArrival();
    },
    destroy() {
      this._disposed = true; clearTimeout(this._timer);
      window.removeEventListener('setup-status', this._onSetup);
      this.$el.removeEventListener('htmx:afterSwap', this._onStarter);
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
    get tvPlaying() {
      return this.connected && this.nowPlaying?.source?.kind === 'tv'
        && !!this.nowPlaying.video_id && this.nowPlaying.paused === false;
    },
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
    _job: null, _completedJob: null, _checkingJob: false,
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
      const scan = () => { if (this._disposed) return;
        if (this.running) {
          if (!this._stream) this.checkJob(this._job);
          return;
        }
        fetch('/sync/active').then(r => r.json())
        .then(d => { if (d && d.job_id) this.attach(d.job_id); }).catch(() => {}); };
      scan();
      this._scan = setInterval(scan, 3000);
    },
    attach(jobId) {
      if (this._disposed || this.running || this._completedJob === jobId) return;
      this._job = jobId; this.running = true; this.failed = false;
      this._stream?.close();
      const es = this._stream = new EventSource(`/sync/events/${jobId}`);
      let errors = 0;
      es.onmessage = (m) => {
        if (this._disposed || this._stream !== es) return;
        errors = 0;
        const ev = JSON.parse(m.data);
        if (ev.type === 'err' || ev.type === 'auth_expired') this.failed = true;
        if (ev.type === 'end') {
          this.finishJob(jobId, ev);
          return;
        }
      };
      es.onerror = () => {
        if (this._disposed || this._stream !== es) return;
        // Native reconnect retains Last-Event-ID and can replay completion while
        // /sync/active is already empty. Transport loss is not a failed sync.
        if (es.readyState === EventSource.CLOSED || ++errors >= 5) {
          es.close(); this._stream = null;
          // Keep the known job until its terminal status or expiry is observed.
          // The existing scan polls status after the bounded stream retries.
          this.checkJob(jobId);
        }
      };
    },
    async checkJob(jobId) {
      if (this._disposed || this._checkingJob || this._job !== jobId) return;
      this._checkingJob = true;
      try {
        const response = await fetch(`/sync/status/${jobId}`);
        if (this._disposed || this._job !== jobId) return;
        if (response.status === 404) {
          this.running = false; this._completedJob = jobId;
        } else if (response.ok) {
          const state = await response.json();
          if (!this._disposed && this._job === jobId && state.done) this.finishJob(jobId, state);
        }
      } catch (_) { /* The next scan retries this same known job. */ }
      finally { this._checkingJob = false; }
    },
    finishJob(jobId, ev) {
      if (this._disposed || this._job !== jobId || this._completedJob === jobId) return;
      this._stream?.close(); this._stream = null;
      this.running = false; this._completedJob = jobId;
      if (ev.outcome === 'complete' && !ev.error && !this.failed) {
        if (!this.$el.querySelector('#onboarding-feed')) {
          htmx.ajax('GET', '/', {target: '#home', select: '#home', swap: 'outerHTML'}).catch(() => {});
        }
      } else {
        this.failed = true;
      }
    },
  };
}
