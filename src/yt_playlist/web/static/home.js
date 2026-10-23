function onboardingArrival() {
  return {
    songs: 0, playlists: 0, albums: 0, covers: [], outcome: null, statusError: '',
    imported: false, analyzed: 0, total: 0, genres: 0, model_tracks: 0, modes: 0, discoveries: 0,
    _timer: null, _disposed: false,
    applyArrival(data) {
      this.songs = data.songs; this.playlists = data.playlists; this.albums = data.albums;
      this.outcome = data.outcome;
      Object.assign(this, data.setup || {});
      // Keep arrived covers in place rather than reshuffling the graphic on each poll.
      this.covers = [...new Set([...this.covers, ...data.covers])].slice(0, 6);
      this.statusError = '';
      if (data.setup?.complete && data.setup.mixes_ready && !this._openingHome) {
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
  // Home consumes the document's shared playback status. Library syncing is automatic in the
  // background, so the only action here is an
  // unobtrusive "Refresh library" link that kicks a manual POST /sync for power users.
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
    refreshing: false,
    running: false,        // a library sync is streaming into the console
    failed: false,
    lines: [],
    currentStep: '',
    _job: null,            // job id the console is attached to (guards against double-attach)
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
      window.playbackStatus.start();
      if (window.playbackStatus.snapshot) this.applyBridgeStatus(window.playbackStatus.snapshot);
      // Attach the live console to whatever library sync is running — the automatic background sync
      // included — so the initial sync's results stream in with no manual action.
      const scan = () => { if (this._disposed || this.running) return; fetch('/sync/active').then(r => r.json())
        .then(d => { if (d && d.job_id) this.attach(d.job_id); }).catch(() => {}); };
      scan();
      this._scan = setInterval(scan, 3000);
      // A completed import remains inspectable after reload or a backend restart.
      fetch('/sync/recent').then(r => r.json()).then(data => {
        if (this._disposed || this._job || this.running) return;
        for (const event of data.events || []) this.push(event);
        this.failed = ['partial', 'failed', 'skipped'].includes(data.outcome);
        if (this.failed) this.currentStep = 'Some items will retry automatically.';
      }).catch(() => {});
    },
    push(ev) {
      const bad = ev.type === 'err' || ev.type === 'auth_expired';
      const pip = bad ? '✗' : (ev.type === 'done' || ev.type === 'end') ? '✓' : '•';
      const cls = bad ? 'err' : (ev.type === 'done' || ev.type === 'end') ? 'done' : '';
      this.lines.push({ text: ev.text || '', pip, cls });
      if (ev.text) this.currentStep = ev.text;
      this.$nextTick(() => { const l = this.$refs.log; if (l) l.scrollTop = l.scrollHeight; });
    },
    attach(jobId) {
      if (this._disposed || this.running || this._job === jobId) return;
      this._job = jobId; this.running = true; this.failed = false; this.lines = []; this.currentStep = '';
      this._stream?.close();
      const es = this._stream = new EventSource(`/sync/events/${jobId}`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === 'err' || ev.type === 'auth_expired') this.failed = true;
        if (ev.type === 'end') {
          es.close(); this.running = false;
          if (ev.outcome === 'complete' && !ev.error && !this.failed) {
            this.push({ type: 'done', text: 'Library updated.' });
            // Setup advances through prepared offers, not import completion. Keep
            // its current screen, shared player and open Details mounted.
            if (!this.$el.querySelector('#onboarding-feed')) {
              htmx.ajax('GET', '/', {target: '#home', select: '#home', swap: 'outerHTML'})
                .catch(() => {});
            }
          } else {
            this.failed = true;
            const message = ev.outcome === 'partial'
              ? 'Library partially updated. You can listen to the songs already loaded; missing items will retry in the background.'
              : ev.outcome === 'skipped'
                ? 'Sync skipped. Reconnect YouTube Music and retry.'
                : 'Sync failed. Reconnect or re-authenticate, then retry.';
            this.push({ type: 'err', text: message });
            this.currentStep = ev.outcome === 'partial' ? 'Some items will retry automatically.'
              : 'Check import details to continue.';
          }
          return;
        }
        this.push(ev);
      };
      es.onerror = () => { es.close(); this.running = false; this.failed = true;
        this.push({ type: 'err', text: 'stream interrupted' }); };
    },
    async refresh() {
      if (this.running || this.refreshing) return;
      this.refreshing = true;
      // Kicking off a sync retires the setup flash (same event the old sync bar dispatched).
      window.dispatchEvent(new CustomEvent('sync-started'));
      if (location.search.includes('flash=')) history.replaceState({}, '', location.pathname);
      try {
        const r = await fetch('/sync', { method: 'POST' });
        const job = (await r.json()).job_id;
        if (job) this.attach(job);          // stream this one into the console
      } catch (e) {}
      this.refreshing = false;
    },
    rate(action) {
      if (!this.nowPlaying) return;
      const want = action === 'like' ? 'LIKE' : 'DISLIKE';
      const current = this.nowPlaying;
      const previous = current.likeStatus;
      // The next poll reconciles with the backend's confirmed rating.
      this.nowPlaying.likeStatus = this.nowPlaying.likeStatus === want ? 'INDIFFERENT' : want;
      fetch('/now-playing/rate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, videoId: this.nowPlaying.video_id,
                               likeStatus: this.nowPlaying.likeStatus }),
      }).then(r => r.json()).then(d => {
        if (!d.ok && this.nowPlaying === current) current.likeStatus = previous;
      }).catch(() => { if (this.nowPlaying === current) current.likeStatus = previous; });
    },
    toggle() {
      if (!this.nowPlaying) return;
      // Optimistic flip; the next poll reconciles with the real state read back from the player.
      this.nowPlaying.paused = !this.nowPlaying.paused;
      fetch('/now-playing/toggle', { method: 'POST' }).catch(() => {});
    },
  };
}
