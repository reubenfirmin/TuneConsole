const homeAreaLoads = new WeakMap();

function loadHomeArea(element) {
  if (element.dataset.loaded || homeAreaLoads.has(element)) return;
  const panel = element.closest('[data-dash]').dataset.dash;
  const dashboard = element.closest('.home-dashboard');
  const load = {timer: null, fragment: false};
  homeAreaLoads.set(element, load);
  const active = () => homeAreaLoads.get(element) === load &&
    element.isConnected && Alpine.$data(dashboard).panel === panel;
  const loadAssets = async attempt => {
    if (!active()) return;
    try {
      const assets = JSON.parse(document.getElementById('home-area-assets').textContent);
      await loadHomeAssets(assets.styles[panel] || [], assets.scripts[panel] || [], assets.version, active);
    } catch (error) {
      if (!active()) return;
      console.debug('[Home tab] assets unavailable', error);
      if (attempt < 2) load.timer = setTimeout(() => loadAssets(attempt + 1), 350 * 2 ** attempt);
      else homeAreaLoads.delete(element);
      return;
    }
    if (!active()) return;
    // Alpine can initialize a swapped Home before HTMX settles its descendants.
    // Register the fragment listener before sending the area's first open event.
    load.fragment = true;
    htmx.process(element);
    htmx.trigger(element, 'home-area-open');
  };
  loadAssets(0);
}

function cancelHomeArea(element) {
  if (element.hasAttribute('data-refresh-on-open')) delete element.dataset.loaded;
  const load = homeAreaLoads.get(element);
  if (!load) return;
  homeAreaLoads.delete(element);
  clearTimeout(load.timer);
  if (load.fragment) cancelReadFragment(element);
}

function mountHomeTv(element) {
  if (document.querySelector('body > .tv-session')) return;
  const session = element.querySelector('.tv-session-template');
  if (session) document.body.append(session.content.cloneNode(true));
}

function finishHomeArea(element, event) {
  if (!homeAreaLoads.has(element)) return;
  const status = event.detail.xhr.status;
  // The shared fragment recovery owns these retries until the tab closes.
  if (status === 0 || status === 429 || status >= 500) return;
  homeAreaLoads.delete(element);
  if (status !== 200 || event.detail.xhr.getResponseHeader('X-Read-Pending')) return;
  element.dataset.loaded = '1';
  window.dispatchEvent(new Event('home-area-loaded'));
}

function homeRefreshUrl() {
  const area = location.hash.slice(1);
  return area ? '/?' + new URLSearchParams({panel: area.startsWith('gen-') ? 'directions' : area}) : '/';
}

function onboardingArrival(artistsRequired = false) {
  return {
    songs: 0, playlists: 0, albums: 0, covers: [], outcome: null, statusError: '',
    // Import progress belongs to this view, separate from Home's SSE attachment.
    running: false,
    imported: false, analyzed: 0, total: 0, genres: 0, model_tracks: 0, modes: 0, discoveries: 0,
    listening_format: 'mixes', activity: '', current: null, target: null, starterReady: false, starterPlayed: false,
    generationStarted: false,
    artists_required: artistsRequired,
    _timer: null, _homeRetry: null, _homeRetryDelay: 1000, _disposed: false, _suspended: false,
    _requests: new AbortController(), _readingArrival: false,
    get choosingArtists() { return this.artists_required && !this.starterReady; },
    get progressLabel() {
      if (this.statusError) return this.statusError;
      if (!this.connected && !this.running) return 'Open YouTube Music to continue setup';
      if (this.running && this.current && this.target && ['playlists', 'albums', 'songs'].includes(this.activity)) {
        return 'Reading ' + ({albums: 'album ', playlists: 'playlist ', songs: 'song '}[this.activity])
          + this.current + ' of ' + this.target;
      }
      if (!this.imported && !this.songs) return 'Bringing in your music';
      if (this.listening_format === 'albums') return 'Finding your first album';
      if (this.listening_format === 'radio') return 'Finding songs for your first station';
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
      if (this._disposed || this._suspended) return;
      this.songs = data.songs; this.playlists = data.playlists; this.albums = data.albums;
      this.outcome = data.outcome;
      Object.assign(this, data.setup || {});
      // Keep arrived covers in place rather than reshuffling the graphic on each poll.
      this.covers = [...new Set([...this.covers, ...data.covers])].slice(0, 6);
      this.statusError = '';
      if (data.setup?.complete && (data.setup.offers_ready || data.setup.mixes_ready || data.setup.albums_ready)
          && !this._openingHome) {
        this._openingHome = true;
        // Load the newly selected area's assets before mounting it. The shared
        // player stays outside the replaceable page.
        refreshHomePage()
          .then(() => {
            // A recoverable read can settle before the ready feed arrives.
            if (document.querySelector('#onboarding-feed')) throw new Error('Home did not arrive.');
            document.querySelector('main.page-home')?.classList.remove('page-setup');
          })
          .catch(() => {
            this._openingHome = false;
            if (this._disposed || this._suspended) return;
            // The document's setup poll stops once offers are ready. Retry the
            // failed fragment independently, with a capped backoff, until it lands.
            clearTimeout(this._homeRetry);
            this._homeRetry = setTimeout(() => this.applyArrival(data), this._homeRetryDelay);
            this._homeRetryDelay = Math.min(15000, this._homeRetryDelay * 2);
          });
      }
    },
    async refreshArrival() {
      if (this._disposed || this._suspended || this._readingArrival) return;
      this._readingArrival = true;
      const requests = this._requests;
      clearTimeout(this._timer);
      try {
        const response = await fetch('/home/onboard/status', {signal: requests.signal});
        if (!response.ok) throw new Error('Import status could not be loaded.');
        const data = await response.json();
        if (this._disposed || this._suspended || requests !== this._requests) return;
        this.applyArrival(data);
      } catch (_) {
        if (!this._disposed && !this._suspended && requests === this._requests)
          this.statusError = 'Reconnecting to import progress…';
      } finally {
        if (requests === this._requests) {
          this._readingArrival = false;
          if (!this._disposed && !this._suspended && !this.complete)
            this._timer = setTimeout(() => this.refreshArrival(), 3000);
        }
      }
    },
    init() {
      this._onLeave = () => this.destroy();
      this._onHide = event => {
        if (!event.persisted) { this.destroy(); return; }
        this._suspended = true;
        this._requests.abort();
        clearTimeout(this._timer); clearTimeout(this._homeRetry);
      };
      this._onShow = event => {
        if (!event.persisted) return;
        this._suspended = false;
        this._requests = new AbortController();
        this._readingArrival = false;
        if (window.setupStatus) {
          if (window.setupStatus.snapshot) this.applyArrival(window.setupStatus.snapshot);
        } else this.refreshArrival();
      };
      window.addEventListener('app-page-leaving', this._onLeave);
      window.addEventListener('pagehide', this._onHide);
      window.addEventListener('pageshow', this._onShow);
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
      this._requests.abort();
      window.removeEventListener('app-page-leaving', this._onLeave);
      window.removeEventListener('pagehide', this._onHide);
      window.removeEventListener('pageshow', this._onShow);
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
    _scan: null, _stream: null, _disposed: false, _suspended: false, _initialized: false,
    _requests: new AbortController(), _scanning: false,
    destroy() {
      this._disposed = true;
      this._requests.abort();
      clearInterval(this._scan);
      this._stream?.close();
      window.removeEventListener('app-page-leaving', this._onLeave);
      window.removeEventListener('pagehide', this._onHide);
      window.removeEventListener('pageshow', this._onShow);
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
      this._onLeave = () => this.destroy();
      window.addEventListener('app-page-leaving', this._onLeave);
      this.$el.closest('main.page-home')?.classList.toggle('page-setup', !!this.$el.querySelector('#onboarding-feed'));
      window.playbackStatus.start();
      if (window.playbackStatus.snapshot) this.applyBridgeStatus(window.playbackStatus.snapshot);
      // Watch completion without surfacing routine background retries as user actions.
      const scan = () => { if (this._disposed || this._suspended) return;
        if (this._refreshPending) this.refreshOffers();
        if (this.running) {
          if (!this._stream) this.checkJob(this._job);
          return;
        }
        if (this._scanning) return;
        this._scanning = true;
        const requests = this._requests;
        fetch('/sync/active', {signal: requests.signal}).then(r => r.json())
        .then(d => {
          if (!this._suspended && requests === this._requests && d?.job_id) this.attach(d.job_id);
        }).catch(() => {})
        .finally(() => { if (requests === this._requests) this._scanning = false; }); };
      scan();
      this._scan = setInterval(scan, 3000);
      this._onHide = event => {
        if (!event.persisted) { this.destroy(); return; }
        this._suspended = true;
        this._requests.abort();
        clearInterval(this._scan);
        this._stream?.close(); this._stream = null;
      };
      this._onShow = event => {
        if (!event.persisted) return;
        this._suspended = false;
        this._requests = new AbortController();
        this._scanning = this._checkingJob = false;
        scan();
        this._scan = setInterval(scan, 3000);
      };
      window.addEventListener('pagehide', this._onHide);
      window.addEventListener('pageshow', this._onShow);
    },
    attach(jobId) {
      if (this._disposed || this._suspended || this.running || this._completedJob === jobId) return;
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
      if (this._disposed || this._suspended || this._checkingJob || this._job !== jobId) return;
      this._checkingJob = true;
      const requests = this._requests;
      try {
        const response = await fetch(`/sync/status/${jobId}`, {signal: requests.signal});
        if (this._disposed || this._suspended || requests !== this._requests || this._job !== jobId) return;
        if (response.status === 404) {
          this.running = false; this._completedJob = jobId;
        } else if (response.ok) {
          const state = await response.json();
          if (!this._disposed && !this._suspended && requests === this._requests
              && this._job === jobId && state.done) this.finishJob(jobId, state);
        }
      } catch (_) { /* The next scan retries this same known job. */ }
      finally { if (requests === this._requests) this._checkingJob = false; }
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
    async refreshOffers() {
      if (this._disposed || this._suspended || this._refreshing) return;
      const root = this.$el;
      const boardIdle = window.homeBoardRefreshGuard?.(root);
      if (boardIdle && !boardIdle()) return;
      const url = homeRefreshUrl();
      const signal = AbortSignal.any([this._requests.signal, AbortSignal.timeout(10000)]);
      const active = () => !signal.aborted && root.isConnected && url === homeRefreshUrl();
      this._refreshing = true;
      try {
        const response = await fetch(url, {signal, headers: {'X-Home-Refresh': 'sync'}});
        if (!response.ok) return;
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const next = page.getElementById('home');
        if (!next || next.querySelector('#onboarding-feed') || !active()) return;
        const panel = next.dataset.homePanel;
        const assets = JSON.parse(page.getElementById('home-area-assets').textContent);
        await loadHomeAssets(assets.styles[panel] || [], assets.scripts[panel] || [], assets.version, active);
        if (!active() || (boardIdle && !boardIdle())) return;
        Alpine.destroyTree(root);
        htmx.swap(root, next.outerHTML, {swapStyle: 'outerHTML', settleDelay: 0});
        const tv = page.querySelector('body > .tv-session');
        if (tv && !document.querySelector('body > .tv-session')) document.body.append(tv);
      } catch (error) {
        if (!signal.aborted) console.debug('[Home sync] refresh unavailable', error);
      } finally {
        // A guarded or failed refresh stays pending for the next scan.
        this._refreshing = false;
      }
    },
  };
}
