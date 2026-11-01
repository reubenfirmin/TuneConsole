function onboardingArrival(artistsRequired = false) {
  return {
    songs: 0, playlists: 0, albums: 0, covers: [], outcome: null, statusError: '',
    // Import progress belongs to this view, separate from Home's SSE attachment.
    running: false,
    imported: false, analyzed: 0, total: 0, genres: 0, model_tracks: 0, modes: 0, discoveries: 0,
    listening_format: 'mixes', activity: '', current: null, target: null, starterReady: false, starterPlayed: false,
    generationStarted: false,
    artists_required: artistsRequired,
    _timer: null, _homeRetry: null, _homeRetryDelay: 1000, _disposed: false,
    get choosingArtists() { return this.artists_required && !this.starterReady; },
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
    showStarter() {
      const stage = this.$root.querySelector('.setup-listening');
      if (this.choosingArtists) stage?.querySelector('[data-onboarding-artists]')?.dispatchEvent(new CustomEvent('artist-choose'));
      stage?.scrollIntoView({behavior: 'smooth', block: 'start'});
      this.$nextTick(() => stage?.querySelector(this.choosingArtists ? '#artist-start-search' : '[data-onboarding-play], [data-artist-resuming="true"] a')?.focus({preventScroll: true}));
    },
    frameIntro() {
      if (this._introFramed || window.scrollY > 0) return;
      this.$nextTick(() => {
        if (this._disposed || this._introFramed || window.scrollY > 0 || !(this.starterReady || this.choosingArtists) || !this.connected) return;
        const intro = this.$el.querySelector('.festival-intro');
        const tent = intro?.querySelector('.festival-destination[data-first-sounds] .landmark-hit');
        if (!tent || tent.getBoundingClientRect().bottom <= innerHeight) return;
        // Never trade away an active player's controls to frame the scenery.
        if (document.querySelector('.global-now-playing')?.getClientRects().length) return;
        const headerBottom = document.querySelector('.topbar')?.getBoundingClientRect().bottom || 0;
        const distance = intro.getBoundingClientRect().top - headerBottom - 8;
        if (distance > 0) {
          this._introFramed = true;
          window.scrollBy({top: distance, behavior: 'instant'});
        }
      });
    },
    applyArrival(data) {
      if (this._disposed) return;
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
          .then(() => {
            // HTMX resolves HTTP errors as well as successful responses. The
            // required result is a mounted Home feed, not a settled request.
            if (document.querySelector('#onboarding-feed')) throw new Error('Home did not arrive.');
            document.querySelector('main.page-home')?.classList.remove('page-setup');
          })
          .catch(() => {
            this._openingHome = false;
            if (this._disposed) return;
            // The document's setup poll stops once offers are ready. Retry the
            // failed fragment independently, with a capped backoff, until it lands.
            clearTimeout(this._homeRetry);
            this._homeRetry = setTimeout(() => this.applyArrival(data), this._homeRetryDelay);
            this._homeRetryDelay = Math.min(15000, this._homeRetryDelay * 2);
          });
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
      // Confirmation cues the player immediately. The welcome changes only
      // when Home is entered again, including a click on its active tab.
      this.starterPlayed = document.documentElement.dataset.onboardingPlayed === 'true';
      // The starter arrives independently through HTMX, often before sync finishes.
      this._onStarter = () => {
        const artists = this.$el.querySelector('[data-onboarding-artists]');
        if (artists) this.artists_required = true;
        this.starterReady = artists
          ? artists.dataset.artistStage === 'listen' && !!artists.querySelector('.artist-first-set, [data-generated-playlist], [data-artist-resuming="true"]')
          : !!this.$el.querySelector('.starter-mix, [data-onboarding-starter]');
        this.frameIntro();
      };
      this.$el.addEventListener('htmx:afterSwap', this._onStarter);
      this.$el.addEventListener('artist-stage', this._onStarter);
      this._onArtistStarted = () => { this.artists_required = false; this.refresh(); };
      this.$el.addEventListener('artist-started', this._onArtistStarted);
      this._onStarter();
      this._onGeneration = event => {
        if (this.$el.contains(event.detail.elt) && event.detail.requestConfig?.path === '/home/generate') {
          // A disconnected player must not hide an in-flight save or its result.
          this.generationStarted = true;
        }
      };
      document.addEventListener('htmx:beforeRequest', this._onGeneration);
      this._onFrame = () => this.frameIntro();
      window.addEventListener('bridge-status', this._onFrame);
      this._onHome = event => {
        if (!(this.starterReady || this.choosingArtists) || !this.connected || event.button !== 0 || event.metaKey
            || event.ctrlKey || event.shiftKey || event.altKey
            || !event.target.closest('.topbar a[href="/"]')) return;
        event.preventDefault();
        this.starterPlayed = document.documentElement.dataset.onboardingPlayed === 'true';
        this.refresh();
        this.showStarter();
      };
      document.addEventListener('click', this._onHome);
      if (window.setupStatus) {
        this._onSetup = event => this.applyArrival(event.detail);
        window.addEventListener('setup-status', this._onSetup);
        if (window.setupStatus.snapshot) this.applyArrival(window.setupStatus.snapshot);
      } else this.refreshArrival();
    },
    destroy() {
      this._disposed = true; clearTimeout(this._timer); clearTimeout(this._homeRetry);
      window.removeEventListener('setup-status', this._onSetup);
      this.$el.removeEventListener('htmx:afterSwap', this._onStarter);
      this.$el.removeEventListener('artist-stage', this._onStarter);
      this.$el.removeEventListener('artist-started', this._onArtistStarted);
      document.removeEventListener('htmx:beforeRequest', this._onGeneration);
      document.removeEventListener('click', this._onHome);
      window.removeEventListener('bridge-status', this._onFrame);
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
    _job: null, _completedJob: null, _checkingJob: false, _refreshPending: false,
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
        if (this._refreshPending) this.refreshOffers();
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
          this._refreshPending = true;
          this.refreshOffers();
        }
      } else {
        this.failed = true;
      }
    },
    refreshOffers() {
      if (this._disposed || this._refreshing || window.genHomeBusy?.(this.$el)) return;
      this._refreshing = true;
      htmx.ajax('GET', '/', {target: '#home', select: '#home', swap: 'outerHTML',
        headers: {'X-Home-Refresh': 'sync'}})
        .catch(() => {})
        .finally(() => { this._refreshing = false; });
      // A successful replacement disposes this instance. A guarded swap or
      // transport failure leaves the pending refresh for the next normal scan.
    },
  };
}
