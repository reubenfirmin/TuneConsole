/* Home's saved-playlist Radio Session. Playback and additions continue server-side. */
function radioHome(initial = {}) {
  let cached = {};
  let pendingLaunch = null;
  let selectionIntent = null;
  let tuningIntent = null;
  let coverLoad = null;
  let cancelSeedFocus = null;
  try { cached = JSON.parse(sessionStorage.getItem('radio-view') || '{}'); } catch (_) {}
  try {
    const saved = JSON.parse(sessionStorage.getItem('radio-launch'));
    if (saved?.version === 1 && typeof saved.id === 'string' && saved.song &&
        (saved.fromPlaylist ? Number.isInteger(saved.song.id) : typeof saved.song.video_id === 'string')) pendingLaunch = saved;
  } catch (_) {}
  try {
    const saved = JSON.parse(sessionStorage.getItem('radio-tuning'));
    if (saved?.version === 1 && typeof saved.playlistId === 'string' && saved.values &&
        (saved.values.journey == null || typeof saved.values.journey === 'string') &&
        ['comfort', 'theme', 'energy'].every(key => Number.isFinite(saved.values[key]) &&
          saved.values[key] >= 0 && saved.values[key] <= 1)) {
      tuningIntent = {...saved, status: saved.status === 'rejected' ? 'rejected' : 'unconfirmed'};
    }
  } catch (_) {}
  try {
    const saved = JSON.parse(sessionStorage.getItem('radio-selection'));
    if (saved?.version === 1 && typeof saved.playlistId === 'string' &&
        Number.isInteger(saved.slot) && Number.isInteger(saved.serial) &&
        typeof saved.proposal_id === 'string') selectionIntent = {...saved, unconfirmed: true};
  } catch (_) {}
  // Cached artwork is useful; a cached session is not evidence of playback.
  const state = initial.state || {active: false};
  const seeds = initial.seeds?.length ? initial.seeds : cached.seeds || [];
  return Object.defineProperties({
    state, seeds, visible: false, loading: !seeds.length, busy: false, resolving: !!state.resolving,
    loaded: !!(initial.seeds?.length && initial.seedMenu), seedRequest: null, seedRetryAt: 0,
    timer: null, playlistKey: '', playlistSessionId: null, destroyed: false,
    playlistRequest: null,
    busyAction: '', seedMenu: initial.seedMenu || cached.seedMenu, seenMenu: null, currentVideo: null, requestSerial: 0,
    recordPlayback: null, pendingRecord: null, recordCoverUrl: '',
    launchSong: pendingLaunch ? null : initial.launchPlaylist || (state.active && state.starting ? state.tracks?.[0] || {} : null),
    launchPlaylist: initial.launchPlaylist || null,
    launchPreviousId: pendingLaunch?.previousId ?? null,
    launchAttempt: pendingLaunch?.id || null,
    launchRequest: pendingLaunch?.request || null,
    launchUncertain: !!pendingLaunch,
    launchSerial: 0,
    recoverySong: pendingLaunch?.song || null,
    recoveryFromPlaylist: pendingLaunch?.fromPlaylist || false,
    selectedSlot: null, switchChoice: null, switchSending: false, switchSerial: selectionIntent?.serial || 0,
    selectionIntent, selectionError: '', selectionSlot: selectionIntent?.slot ?? null, pendingSong: null,
    hoveredSlot: null, focusedSlot: null,
    tuningDraft: {comfort: 50, theme: 50, energy: 50}, tuningJourney: '', tuningBusy: false, tuningIntent, tuningSerial: 0,
    tuningAxes: [
      {key: 'comfort', low: 'Discovery', high: 'Comfort', color: 'var(--radio-queue-1)'},
      {key: 'theme', low: 'Eclectic', high: 'Themed', color: 'var(--radio-queue-2)'},
      {key: 'energy', low: 'Chill', high: 'Full On', color: 'var(--radio-queue-3)'},
    ],
    get recordTrack() {
      const history = this.state.history || (this.state.tracks || []).slice(0, this.state.history_count || 0);
      const pending = this.pendingRecord;
      const video = pending && Date.now() < pending.until ? pending.videoId : this.recordPlayback?.video_id || this.currentVideo ||
        this.state.tracks?.[this.state.position]?.video_id || history.at(-1)?.video_id;
      const track = [...history, ...(this.state.tracks || [])].find(song => song.video_id === video);
      const playing = this.recordPlayback?.video_id === video ? this.recordPlayback : null;
      return track?.thumbnail ? track : playing || track;
    },
    get recordCover() {
      const cover = this.recordTrack?.thumbnail;
      if (!cover) return '';
      // The record is larger than a table thumbnail. Request the selected track's own image
      // at display size, preserving its identity.
      try {
        const url = new URL(cover, location.href);
        if (url.hostname.endsWith('.googleusercontent.com')) {
          url.pathname = url.pathname.replace(/=w\d+-h\d+(?=-|$)/, '=w544-h544');
        }
        return url.href;
      } catch (_) { return ''; }
    },
    get recordCoverIdentity() {
      return JSON.stringify([this.state.active ? this.sessionId() : null,
        this.recordTrack?.video_id, this.recordTrack?.thumbnail, this.recordCover]);
    },
    syncRecordCover(element) {
      const key = this.recordCoverIdentity;
      if (coverLoad?.key === key) return coverLoad.promise;
      // A replacement SVG reports its own load, including on a cache hit.
      if (element) element.removeAttribute('data-loaded');
      coverLoad?.cancel();
      this.recordCoverUrl = '';
      if (!this.state.active || !this.recordCover || this.destroyed) {
        coverLoad = null;
        return Promise.resolve();
      }
      // An enlarged Google image can fail even when the row's original works.
      // Keep successful fallback artwork across ordinary status/playlist polls.
      const original = new URL(this.recordTrack.thumbnail, location.href).href;
      const candidates = [...new Set([this.recordCover, original])];
      const load = {key, index: 0, image: null, timer: null, url: ''};
      coverLoad = load;
      let resolve;
      load.promise = new Promise(done => { resolve = done; });
      const current = () => !this.destroyed && coverLoad === load && this.recordCoverIdentity === key;
      const cleanup = () => {
        clearTimeout(load.timer);
        if (load.image) load.image.onload = load.image.onerror = null;
      };
      load.cancel = () => { cleanup(); resolve(); };
      load.loaded = element => {
        if (!current() || element.isConnected === false ||
            element.getAttribute('href') !== load.url || this.recordCoverUrl !== load.url) return;
        element.setAttribute('data-loaded', load.url);
        cleanup();
        resolve();
      };
      const next = () => {
        cleanup();
        if (!current()) { resolve(); return; }
        this.recordCoverUrl = '';
        const url = candidates[load.index++];
        load.url = url || '';
        if (!url) { resolve(); return; }
        const image = new Image();
        load.image = image;
        image.referrerPolicy = 'no-referrer';
        image.onload = () => {
          if (load.image !== image) return;
          if (!current()) { load.cancel(); return; }
          if (!image.naturalWidth) { next(); return; }
          // Only expose loaded, current-track artwork. The native SVG load
          // below, rather than this preloader alone, completes preparation.
          this.recordCoverUrl = url;
        };
        const failed = () => { if (load.image === image) next(); };
        image.onerror = failed;
        load.timer = setTimeout(() => {
          if (load.image !== image) return;
          // The last candidate may still arrive later. Stop delaying the
          // session, while retaining its guarded load handler for recovery.
          if (current() && load.index === candidates.length) resolve();
          else next();
        }, 8000);
        image.src = url;
      };
      load.failed = element => {
        if (!current() || element.isConnected === false ||
            element.getAttribute('href') !== load.url || this.recordCoverUrl !== load.url) return;
        element.removeAttribute('data-loaded');
        next();
      };
      next();
      return load.promise;
    },
    recordCoverLoaded(element) { coverLoad?.loaded(element); },
    recordCoverError(element) { coverLoad?.failed(element); },
    previewRecord({url, requestId}) {
      if (!this.state.active) return;
      const target = new URL(url, location.href);
      const videoId = target.searchParams.get('v');
      if (target.hostname !== 'music.youtube.com' || !videoId ||
          (target.searchParams.has('list') && target.searchParams.get('list') !== this.state.playlist_id) ||
          ![...(this.state.history || []), ...(this.state.tracks || [])].some(song => song.video_id === videoId)) return;
      const previousVideo = this.recordPlayback?.video_id || this.currentVideo ||
        this.pendingRecord?.previousVideo || this.recordTrack?.video_id;
      this.pendingRecord = {requestId, videoId, previousVideo, until: Date.now() + 8000};
    },
    settleRecord({requestId, ok}) {
      if (!ok && this.pendingRecord?.requestId === requestId) this.pendingRecord = null;
    },
    syncRecordPlayback(data) {
      const song = data?.connected && data.now_playing;
      if (!this.state.active || !song?.video_id || song.playlist_ytm_id !== this.state.playlist_id) return;
      const pending = this.pendingRecord;
      if (pending && (song.video_id === pending.videoId || song.video_id !== pending.previousVideo ||
          Date.now() >= pending.until)) this.pendingRecord = null;
      this.recordPlayback = {...song};
      this.currentVideo = song.video_id;
      if (this.launchSong && song.paused === false && this.sessionId() !== this.launchPreviousId) {
        this.finishLaunch();
        if (!this.launchSong) {
          this.publishSessionState();
          this.$nextTick(() => this.fitSpace());
        }
      }
      this.settleSongPlayback(song);
    },
    isPendingSong(branch, song) {
      return this.pendingSong?.slot === branch.slot && this.pendingSong.videoId === song.video_id;
    },
    isSkippingSong(branch, song) {
      return this.pendingSong?.slot === branch.slot && this.pendingSong.skipped.includes(song.video_id);
    },
    get selectionMessage() {
      return this.selectionError || (this.selectionIntent?.unconfirmed ? this.unconfirmedSelectionMessage : '') ||
        (this.pendingSong && !this.pendingSong.accepted ? '' : this.state.selection_error ?
          'Playback not confirmed. Check the current song before choosing again.' : '');
    },
    get unconfirmedSelectionMessage() {
      return this.selectionIntent?.video_id
        ? 'Playback not confirmed. Check the current song before choosing again.'
        : 'Stream change not confirmed. Check the active stream before choosing again.';
    },
    saveSelectionIntent() {
      try {
        if (this.selectionIntent) sessionStorage.setItem('radio-selection',
          JSON.stringify({version: 1, ...this.selectionIntent}));
        else sessionStorage.removeItem('radio-selection');
      } catch (_) {}
    },
    clearSelectionIntent() {
      this.selectionIntent = null;
      this.selectionError = '';
      this.saveSelectionIntent();
    },
    get streamsPending() {
      return this.state.active && !this.sessionStarting && !this.state.exhausted && !this.state.directions?.length;
    },
    get displayDirections() {
      if (this.streamsPending) {
        const upcoming = (this.state.tracks || []).slice(Math.max(0, this.state.position ?? 0) + 1);
        return Array.from({length: 4}, (_, slot) => ({slot, active: slot === 0, preparing: true,
          tracks: slot === 0 ? upcoming : []}));
      }
      const pending = this.pendingSong;
      return (this.state.directions || []).map(branch => {
        if (branch.slot !== pending?.slot) return branch;
        // Keep the skip preview visible until playback catches up with a queue
        // refresh that has already removed those rows from the offered stream.
        const prefix = new Set(pending.preview.map(song => song.video_id));
        return {...branch, tracks: [...pending.preview, ...branch.tracks.filter(song => !prefix.has(song.video_id))]};
      });
    },
    songAvailable(branch, song) {
      return this.state.directions?.some(current => current.slot === branch.slot &&
        current.tracks.some(track => track.video_id === song.video_id));
    },
    settleSongPlayback(song) {
      if (this.pendingSong && song?.playlist_ytm_id === this.pendingSong.playlistId &&
          song.video_id === this.pendingSong.videoId && song.paused === false) this.pendingSong = null;
      const intent = this.selectionIntent;
      if (intent?.video_id && song?.playlist_ytm_id === intent.playlistId &&
          song.video_id === intent.video_id && song.paused === false) this.clearSelectionIntent();
    },
    openTuning() {
      const values = this.tuningIntent?.playlistId === this.sessionId()
        ? this.tuningIntent.values : this.state.tuning || {comfort: .5, theme: .5, energy: .5};
      this.tuningDraft = Object.fromEntries(this.tuningAxes.map(({key}) => [key, Math.round(values[key] * 100)]));
      this.tuningJourney = values.journey || '';
      this.$refs.tuningDialog.showModal();
      if (this.growth?.vibes_ready === null) this.$dispatch('home-growth-request');
    },
    resetTuning() {
      this.tuningDraft = {comfort: 50, theme: 50, energy: 50};
      this.tuningJourney = '';
    },
    saveTuningIntent() {
      try {
        if (this.tuningIntent) sessionStorage.setItem('radio-tuning', JSON.stringify({version: 1, ...this.tuningIntent}));
        else sessionStorage.removeItem('radio-tuning');
      } catch (_) {}
    },
    get tuningWorking() {
      const intent = this.tuningIntent;
      if (!this.state.active || !intent || intent.playlistId !== this.sessionId()) return false;
      const pending = this.state.tuning_pending;
      return this.tuningBusy || (intent.status !== 'rejected' && pending != null &&
        Object.entries(intent.values).every(([key, value]) => (pending[key] ?? null) === value));
    },
    get tuningMessage() {
      if (this.tuningWorking) return 'Tuning your future songs…';
      if (!this.tuningIntent) return '';
      if (this.tuningIntent.reason === 'energy_locked') return 'Energy tuning is available after Vibes unlocks. Your settings are kept here.';
      return this.tuningIntent.status === 'rejected'
        ? 'These settings were not applied. Your values are kept here to adjust or apply again.'
        : 'These settings are not confirmed yet. Your values are kept here while the session catches up.';
    },
    async applyTuning() {
      if (this.tuningBusy) return;
      const intent = {id: crypto.randomUUID(), playlistId: this.sessionId(), status: 'pending',
        values: Object.fromEntries(Object.entries(this.tuningDraft).map(([key, value]) => [key, value / 100]))};
      if (this.tuningJourney || this.state.tuning?.journey || this.tuningIntent?.values.journey)
        intent.values.journey = this.tuningJourney || null;
      this.tuningIntent = intent;
      this.saveTuningIntent();
      const selectionSerial = this.switchSerial;
      ++this.tuningSerial;
      ++this.requestSerial;
      this.tuningBusy = true;
      this.$refs.tuningDialog.close();
      try {
        const result = await this.json('tune', {method: 'POST', headers: {'Content-Type': 'application/json',
          'X-Radio-Tuning-Client': 'close-and-regenerate-v3'},
          body: JSON.stringify(intent.values)});
        if (this.destroyed || this.tuningIntent?.id !== intent.id) return;
        if (typeof result.ok !== 'boolean') throw new Error('Missing tuning receipt');
        this.tuningIntent = {...intent, status: result.ok ? 'unconfirmed' : 'rejected', reason: result.tune_reason};
        // A choice made while planning owns its selection snapshot. Reconcile
        // the tuning through a fresh status instead of restoring an old menu.
        if (typeof result.active === 'boolean' && selectionSerial === this.switchSerial) this.applyState(result);
        this.saveTuningIntent();
      } catch (error) {
        console.debug('[Radio] tuning deferred', error);
        if (!this.destroyed && this.tuningIntent?.id === intent.id) {
          this.tuningIntent = {...intent, status: 'unconfirmed'};
          this.saveTuningIntent();
        }
      } finally {
        ++this.requestSerial;
        this.tuningBusy = false;
      }
    },
    get activeSlot() { return this.state.directions?.find(branch => branch.active)?.slot ?? 0; },
    get previewSlot() { return this.hoveredSlot ?? this.focusedSlot; },
    get connectedSlot() { return this.previewSlot ?? this.activeSlot; },
    queueColor(slot) { return `var(--radio-queue-${slot % 4})`; },
    /** @returns {RadioArtworkView} */
    get artworkView() {
      return {
        playlistId: this.state.active ? this.state.playlist_id : null,
        visible: this.visible,
        moving: this.visible && this.state.active && !this.sessionStarting && !this.destroyed,
        connectedSlot: this.connectedSlot,
        directionCount: this.state.directions?.length || 4,
      };
    },
    sessionId(state = this.state) { return state.playlist_id || state.local_id; },
    get sessionStarting() { return !!(this.state.starting && this.playlistSessionId !== this.sessionId()); },
    get preparingSession() { return !!(this.state.active && this.sessionId() && this.playlistSessionId !== this.sessionId()); },
    get restoring() { return (this.resolving || this.preparingSession) && !this.launchSong && !this.sessionStarting && this.busyAction !== 'start'; },
    get launching() { return this.resolving || this.busyAction === 'start' || !!this.launchSong || (this.state.active && this.sessionStarting) || this.preparingSession; },
    get showSeeds() { return !this.state.active && !this.launching; },
    get launchTracks() {
      const pending = this.busyAction === 'start' || this.launchSong;
      const established = !pending || (this.state.active && this.sessionId() !== this.launchPreviousId);
      const tracks = [this.launchSong, ...(this.launchPlaylist?.tracks || []),
        ...(established ? (this.state.tracks || []).slice(0, 8) : [])].filter(song => song?.video_id);
      return tracks.filter((song, index) => tracks.findIndex(t => t.video_id === song.video_id) === index).slice(0, 8);
    },
    historyIds(state) {
      return (state.history || (state.tracks || []).slice(0, state.history_count || 0)).map(t => t.video_id);
    },
    tableKey(state) {
      return JSON.stringify([this.sessionId(state), state.metadata_revision || 0, this.historyIds(state)]);
    },
    playlistMatches(state, root = this.$refs.playlist) {
      return !!root?.querySelector('table') && JSON.stringify(
        Array.from(root.querySelectorAll('tr[data-vid]'), row => row.dataset.vid)
      ) === JSON.stringify(this.historyIds(state));
    },
    prepareSession() {
      const state = this.state, sessionId = this.sessionId(state);
      if (this.destroyed || this.resolving || !state.active || this.sessionStarting || !sessionId ||
          this.playlistSessionId === sessionId) return;
      if ((state.position >= 0 && this.streamsPending) ||
          (this.playlistKey === this.tableKey(state) && this.playlistMatches(state))) {
        this.playlistSessionId = sessionId;
      }
    },
    applyState(state) {
      const changed = (state.directions || []).filter(branch => {
        const old = this.state.directions?.find(previous => previous.slot === branch.slot);
        return old && old.id !== branch.id;
      }).map(branch => branch.slot);
      const tuning = this.tuningIntent;
      const sessionId = this.sessionId(state);
      // Missing playback presence can make an ongoing saved session inactive.
      // Only its committed values, a different owner, or Stop settle the intent.
      if (tuning && !state.resolving && (state.stopped === true ||
          (sessionId && sessionId !== tuning.playlistId) ||
          (sessionId === tuning.playlistId && state.tuning_confirmed !== false &&
            Object.keys(tuning.values).every(key => (state.tuning?.[key] ?? null) === tuning.values[key])))) {
        this.tuningIntent = null;
        this.saveTuningIntent();
        this.$refs.tuningDialog?.close();
      }
      if (!state.active || state.playlist_id !== this.state.playlist_id) {
        this.selectedSlot = null;
        if (state.stopped || state.playlist_id !== this.state.playlist_id) this.playlistSessionId = null;
        this.currentVideo = null;
        this.recordPlayback = null;
        this.pendingRecord = null;
        this.pendingSong = null;
        this.selectionSlot = null;
        this.selectionError = '';
      }
      if (state.playlist_id !== this.state.playlist_id) {
        this.playlistKey = '';
        this.$refs.playlist.replaceChildren();
      }
      const intent = this.selectionIntent;
      if (intent && (!state.active || state.playlist_id !== intent.playlistId ||
          (!intent.video_id && !state.updating && (state.directions || []).some(branch =>
            branch.slot === intent.slot && branch.id === intent.proposal_id && branch.active)))) {
        this.clearSelectionIntent();
      }
      if (this.selectionIntent) this.selectionSlot = this.selectionIntent.slot;
      if (this.pendingSong?.accepted && state.selection_error) this.pendingSong = null;
      if (state.browser_age != null && state.browser_age <= 8) this.settleSongPlayback(state.browser?.player);
      if (this.selectedSlot != null) {
        const slot = this.selectedSlot;
        const acknowledged = state.pending_slot === slot || (!state.updating &&
          (state.directions || []).some(branch => branch.slot === slot && branch.active));
        if (acknowledged && !this.switchChoice && !this.switchSending) this.selectedSlot = null;
        else state = {...state, directions: (state.directions || []).map(branch =>
          ({...branch, active: branch.slot === slot}))};
      }
      this.state = state;
      if (state.active) cancelSeedFocus?.();
      this.resolving = !!state.resolving;
      if (state.active && this.sessionStarting && !this.launchSong) this.launchSong = state.tracks?.[0] || {};
      this.finishLaunch();
      this.prepareSession();
      this.publishSessionState();
      this.syncArtworkMotion();
      this.animateQueues(changed.filter(slot => slot !== this.pendingSong?.slot));
    },
    publishSessionState() {
      this.$dispatch('radio-session-state', {active: !!this.state.active, starting: this.sessionStarting,
        playlist: this.state.playlist_id || null});
    },
    finishLaunch() {
      if ((!this.launchSong && !this.launchUncertain) || this.busyAction === 'start' || this.resolving) return;
      // An inactive or previous-session snapshot cannot settle a lost POST.
      // A second click on the same selection reuses the original attempt.
      if (this.launchUncertain && (!this.state.active || this.sessionId() === this.launchPreviousId)) return;
      if (!this.state.active) {
        console.debug('[Radio] launch returned to song choices', this.state.error);
        this.clearLaunch();
      } else if (this.state.playlist_id && (!this.sessionStarting ||
          (this.sessionId() !== this.launchPreviousId && this.recordPlayback?.paused === false &&
           this.recordPlayback.playlist_ytm_id === this.state.playlist_id))) {
        this.playlistSessionId = this.sessionId();
        this.clearLaunch();
      }
    },
    rememberLaunch() {
      try { sessionStorage.setItem('radio-launch', JSON.stringify({version: 1,
        id: this.launchAttempt, song: this.recoverySong, fromPlaylist: this.recoveryFromPlaylist,
        previousId: this.launchPreviousId, request: this.launchRequest})); } catch (_) {}
    },
    clearLaunch() {
      try {
        const saved = JSON.parse(sessionStorage.getItem('radio-launch'));
        if (saved?.id === this.launchAttempt) sessionStorage.removeItem('radio-launch');
      } catch (_) {}
      this.launchAttempt = null;
      this.launchRequest = null;
      this.launchUncertain = false;
      this.launchSong = null;
      this.launchPlaylist = null;
    },
    deferLaunch() {
      this.launchUncertain = true;
      this.launchSong = null;
      this.launchPlaylist = null;
    },
    init() {
      this.syncRecordPlayback(window.playbackStatus?.snapshot);
      this.initArtwork();
      this.publishSessionState();
      const url = new URL(location.href);
      if (url.searchParams.has('radio_playlist')) {
        url.searchParams.delete('radio_playlist');
        history.replaceState(null, '', url.pathname + url.search + url.hash);
      }
      if (this.launchPlaylist) {
        this.$nextTick(() => this.start(this.launchPlaylist, true));
        return;
      }
      this.$nextTick(() => {
        if (!this.state.active) return;
        const key = this.tableKey(this.state);
        if (this.playlistMatches(this.state)) this.playlistKey = key;
        else if (cached.playlistVersion === 2 && cached.playlistKey === key && cached.playlistHtml) {
          htmx.swap(this.$refs.playlist, cached.playlistHtml, {swapStyle: 'innerHTML'},
            {eventInfo: {target: this.$refs.playlist}});
          if (this.playlistMatches(this.state)) this.playlistKey = key;
        }
        this.$refs.playlist.querySelector('.radio-history-space')?.removeAttribute('data-layout');
        this.prepareSession();
        this.fitSpace();
      });
      // The visible Radio panel refreshes its offer in setVisible(). A hidden
      // panel on the unlock map must not start another preparation request.
    },
    cacheView() {
      try { sessionStorage.setItem('radio-view', JSON.stringify({seeds: this.seeds,
        seedMenu: this.seedMenu, playlistVersion: 2, playlistKey: this.playlistKey,
        playlistHtml: this.$refs.playlist.innerHTML})); }
      catch (_) {}
    },
    async loadSeeds() {
      if (!this.visible || this.seedRequest || this.loaded || performance.now() < this.seedRetryAt) return;
      const controller = new AbortController();
      this.seedRequest = controller;
      try {
        const offer = await this.json('seeds', {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]),
        });
        if (this.destroyed || controller.signal.aborted) return;
        this.seeds = offer.tracks;
        this.seedMenu = offer.menu_id;
        this.loaded = offer.tracks.length > 0;
        if (!this.loaded) this.seedRetryAt = performance.now() + 5000;
        this.cacheView();
        this.$nextTick(() => { this.fitSpace(); document.dispatchEvent(new Event('listening-options')); });
      } catch (error) {
        if (!controller.signal.aborted) console.debug('[Radio] seed refresh deferred', error);
      } finally { this.seedRequest = null; this.loading = false; }
    },
    setVisible(value) {
      if (this.visible === value) return;
      this.visible = value;
      this.syncArtworkMotion();
      if (value) {
        this.$nextTick(() => { this.fitSpace(); document.dispatchEvent(new Event('listening-options')); }); this.poll();
      }
      else {
        clearTimeout(this.timer);
        this.timer = null;
        this.seedRequest?.abort();
        cancelSeedFocus?.();
      }
    },
    destroy() {
      this.destroyed = true;
      clearTimeout(this.timer);
      this.seedRequest?.abort();
      this.playlistRequest?.controller.abort();
      coverLoad?.cancel();
      cancelSeedFocus?.();
      this.destroyArtwork();
    },
    async json(path, options) {
      const response = await fetch('/radio/' + path, ['status', 'select', 'tune'].includes(path)
        ? {signal: AbortSignal.timeout(8000), ...options} : options);
      if (!response.ok) throw new Error('Could not reach Radio. Try again.');
      return response.json();
    },
    async refreshPlaylist(state) {
      const key = this.tableKey(state);
      if (!state.active || !this.sessionId(state) || key === this.playlistKey ||
          key === this.playlistRequest?.key) return;
      this.playlistRequest?.controller.abort();
      const controller = new AbortController();
      this.playlistRequest = {key, controller};
      try {
        const response = await fetch('/radio/playlist', {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]),
        });
        if (!response.ok) throw new Error('Could not load the session playlist.');
        const html = await response.text();
        if (this.destroyed || controller.signal.aborted || !this.state.active || key !== this.tableKey(this.state)) return;
        const content = document.createElement('div');
        content.innerHTML = html;
        if (!this.playlistMatches(state, content)) return;
        htmx.swap(this.$refs.playlist, html, {swapStyle: 'innerHTML'},
          {eventInfo: {target: this.$refs.playlist}});
        this.playlistKey = key;
        this.cacheView();
        this.$nextTick(() => {
          this.prepareSession();
          this.fitSpace();
          this.markPlayingRow(this.state);
        });
      } catch (error) {
        if (!controller.signal.aborted) console.debug('[Radio] history refresh deferred', error);
      } finally {
        if (this.playlistRequest?.controller === controller) this.playlistRequest = null;
      }
    },
    markPlayingRow(state) {
      const current = state.browser_age != null && state.browser_age <= 8 &&
        state.browser?.player?.playlist_ytm_id === state.playlist_id ? state.browser.player.video_id : null;
      const rows = Array.from(this.$refs.playlist.querySelectorAll('tr[data-vid]'));
      const playing = rows.findLast(row => row.dataset.vid === current);
      rows.forEach(row => {
        const on = row === playing;
        row.classList.toggle('radio-playing', on);
        if (on) row.setAttribute('aria-current', 'true');
        else row.removeAttribute('aria-current');
      });
      if (current) this.currentVideo = current;
    },
    async refresh() {
      const serial = ++this.requestSerial;
      const state = await this.json('status');
      if (this.destroyed || serial !== this.requestSerial || this.busy) return;
      this.applyState(state);
      if (!this.loaded && !state.active) this.loadSeeds();
      this.loading = false;
      this.refreshPlaylist(state);
      this.cacheView();
      this.$nextTick(() => {
        this.prepareSession();
        this.fitSpace();
        this.markPlayingRow(state);
        const menuId = state.menu_id;
        const exposure = state.directions?.some(branch => branch.filling) ? `${menuId}:heads` : menuId;
        if (this.visible && state.active && menuId && this.seenMenu !== exposure) {
          this.seenMenu = exposure;
          this.json('seen', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({menu_id: menuId})}).catch(() => {
              if (this.seenMenu === exposure) this.seenMenu = null;
            });
        }
      });
    },
    focusSeedWhenReady(previousFocus) {
      cancelSeedFocus?.();
      const seeds = this.$refs.seeds;
      if (!seeds || !this.visible || !this.showSeeds) return;
      const focus = () => {
        const active = document.activeElement;
        if (!this.showSeeds || (active !== previousFocus && active !== document.body)) {
          cancelSeedFocus();
          return;
        }
        const seed = seeds.querySelector('.radio-seed-start:enabled');
        if (seed?.checkVisibility({visibilityProperty: true})) {
          cancelSeedFocus();
          seed.focus();
        }
      };
      // Alpine can reveal the grid after the tick that clears the launch.
      const observer = new MutationObserver(focus);
      cancelSeedFocus = () => {
        observer.disconnect();
        document.removeEventListener('focusin', focus);
        cancelSeedFocus = null;
      };
      observer.observe(seeds, {subtree: true, childList: true, attributes: true,
        attributeFilter: ['style', 'disabled']});
      document.addEventListener('focusin', focus);
      focus();
    },
    async cancelUnconfirmedLaunch() {
      const launchId = this.launchAttempt;
      ++this.requestSerial;
      this.busy = true;
      try {
        const state = await this.json('stop', {method: 'POST', signal: AbortSignal.timeout(8000),
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify({launch_id: launchId})});
        if (this.destroyed || this.launchAttempt !== launchId) return false;
        if (state?.ok !== true || typeof state.active !== 'boolean') throw new Error('Missing Radio stop receipt');
        this.clearLaunch();
        this.applyState(state);
        return true;
      } catch (error) {
        console.debug('[Radio] end attempt not confirmed', error);
        return false;
      } finally {
        this.busy = false;
      }
    },
    async start(song, fromPlaylist = false) {
      if (this.busy || this.destroyed) return;
      const previousFocus = document.activeElement;
      const sameSelection = this.recoveryFromPlaylist === fromPlaylist &&
        (fromPlaylist ? this.recoverySong?.id === song.id : this.recoverySong?.video_id === song.video_id);
      const retry = this.launchUncertain && sameSelection;
      if (this.launchUncertain && !retry && !await this.cancelUnconfirmedLaunch()) {
        if (!this.destroyed) await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
        return;
      }
      cancelSeedFocus?.();
      const clickedAt = Date.now();
      const serial = ++this.launchSerial;
      window.tvPlaybackControls?.release();
      ++this.requestSerial;
      if (!retry) {
        this.launchPreviousId = this.sessionId() ?? null;
        this.launchAttempt = crypto.randomUUID();
        this.launchRequest = null;
      }
      if (!this.launchRequest) {
        const now = new Date(clickedAt);
        const localDate = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'),
                           String(now.getDate()).padStart(2, '0')].join('-');
        this.launchRequest = {...(fromPlaylist ? {playlist_id: song.id} : {video_id: song.video_id, menu_id: this.seedMenu}),
                              local_date: localDate};
      }
      this.launchPlaylist = fromPlaylist ? song : null;
      this.launchSong = song;
      this.launchUncertain = false;
      this.recoverySong = {...song}; this.recoveryFromPlaylist = fromPlaylist;
      // Preserve the request across reloads so a retry can only use its original launch ID.
      this.rememberLaunch();
      this.busy = true; this.busyAction = 'start';
      this.$nextTick(() => this.fitSpace());
      try {
        const result = await this.json('start', {method: 'POST', signal: AbortSignal.timeout(45000), headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({...this.launchRequest, launch_id: this.launchAttempt, clicked_at_ms: clickedAt})});
        if (this.destroyed || serial !== this.launchSerial) return;
        if (typeof result?.ok !== 'boolean') throw new Error('Missing Radio start receipt');
        if (result.launch_outcome === 'uncertain') {
          this.deferLaunch();
          return;
        }
        if (!result.ok) {
          console.debug('[Radio] launch returned to song choices', result.reason);
          this.clearLaunch();
          if (result.refresh_seeds) { this.loaded = false; this.loadSeeds(); }
          return;
        }
        this.applyState(result);
      } catch (error) {
        if (this.destroyed || serial !== this.launchSerial) return;
        console.debug('[Radio] launch request interrupted', error);
        this.deferLaunch();
        this.$nextTick(() => this.fitSpace());
      } finally {
        if (!this.destroyed && serial === this.launchSerial) {
          this.busy = false;
          this.busyAction = '';
          this.finishLaunch();
          if (this.showSeeds) this.focusSeedWhenReady(previousFocus);
          await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
        }
      }
    },
    select(branch, song = null) {
      if (this.busy || branch.preparing || (!song && branch.active) || !this.state.active) return;
      // Static assets can update while the running server still only supports
      // direction selection, which would silently ignore the requested song.
      if (song && this.state.can_play_songs !== true) {
        this.selectionSlot = branch.slot;
        this.selectionError = 'Individual song playback is unavailable right now. You can still choose a stream.';
        console.debug('[Radio] song selection unsupported by the current capability response');
        return;
      }
      ++this.requestSerial;
      this.selectionError = '';
      this.selectionSlot = branch.slot;
      this.selectedSlot = branch.slot;
      this.switchChoice = {proposal_id: branch.id, menu_id: this.state.menu_id,
        slot: branch.slot, playlistId: this.state.playlist_id,
        ...(song ? {video_id: song.video_id} : {}),
        serial: ++this.switchSerial, tuningSerial: this.tuningSerial};
      // Retain the requested identity before sending. Reloads reconcile it with
      // observed state; they never repeat a selection whose delivery is unknown.
      this.selectionIntent = {...this.switchChoice};
      this.saveSelectionIntent();
      this.pendingSong = song ? {videoId: song.video_id, title: song.title, artist: song.artist,
        slot: branch.slot, playlistId: this.state.playlist_id, serial: this.switchSerial, accepted: false,
        preview: branch.tracks.slice(0, branch.tracks.findIndex(track => track.video_id === song.video_id) + 1),
        skipped: branch.tracks.slice(0, branch.tracks.findIndex(track => track.video_id === song.video_id))
          .map(track => track.video_id)} : null;
      this.applyState({...this.state, selection_error: null, pending_slot: branch.slot, updating: true});
      this.cacheView();
      this.sendSelection();
    },
    async requestSelection(choice) {
      // Retry only an explicit stale rejection, never an uncertain delivery.
      // Revalidate the exact song in the same stream and session each time.
      for (let attempt = 0; attempt < 3; attempt++) {
        const result = await this.json('select', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({proposal_id: choice.proposal_id, menu_id: choice.menu_id,
            ...(choice.video_id ? {video_id: choice.video_id} : {})})});
        if (this.destroyed || choice.serial !== this.switchSerial) return null;
        // A newer Apply owns the menu. Never retry an older rejected choice
        // against its replacement, even if the same song is still offered.
        if (result.ok || !result.stale || choice.tuningSerial !== this.tuningSerial)
          return {...result, selectionRefused: false};
        ++this.requestSerial;
        const state = await this.json('status');
        if (this.destroyed || choice.serial !== this.switchSerial) return null;
        if (choice.tuningSerial !== this.tuningSerial) return result;
        this.applyState(state);
        this.cacheView();
        if (!state.active || state.playlist_id !== choice.playlistId) return null;
        const branch = state.directions?.find(branch => branch.slot === choice.slot);
        if (!branch || (choice.video_id ? !branch.tracks.some(song => song.video_id === choice.video_id) :
            branch.id !== choice.proposal_id)) return {ok: false, selectionRefused: true, reason: choice.video_id ?
              'That song is no longer in this stream. Choose another song.' : 'Choose another stream.'};
        choice = {...choice, proposal_id: branch.id, menu_id: state.menu_id};
      }
      return {ok: false, selectionRefused: true, reason: 'Could not start your selection. Try again.'};
    },
    async sendSelection() {
      if (this.switchSending) return;
      this.switchSending = true;
      try {
        // Serialize requests and coalesce rapid clicks to the latest choice.
        // The highlight changes before any network work and remains interactive.
        while (this.switchChoice) {
          const choice = this.switchChoice;
          this.switchChoice = null;
          try {
            const result = await this.requestSelection(choice);
            if (!result || this.destroyed || choice.serial !== this.switchSerial) continue;
            if (result.ok === false && result.selectionRefused) {
              // A confirmed refusal (including a song removed from the fresh
              // menu) is settled. Only lost or malformed receipts are uncertain.
              this.clearSelectionIntent();
              this.selectedSlot = null;
              this.pendingSong = null;
              this.selectionError = result.reason || 'Choose another stream.';
              continue;
            }
            if (result.ok !== true) throw new Error(result.reason || 'Missing selection receipt');
            this.clearSelectionIntent();
            if (this.pendingSong?.serial === choice.serial) this.pendingSong.accepted = true;
            // The accepted stream/song still counts. Its snapshot predates a
            // newer Apply, however, and must not restore the old tuning/menu.
            if (choice.tuningSerial === this.tuningSerial) this.applyState(result);
            this.cacheView();
          } catch (error) {
            if (!this.destroyed && choice.serial === this.switchSerial) {
              this.selectedSlot = null;
              this.pendingSong = null;
              this.selectionIntent = {...choice, unconfirmed: true};
              this.saveSelectionIntent();
              this.selectionError = '';
            }
            console.debug('[Radio] direction refresh deferred', error);
          }
        }
      } finally {
        this.switchSending = false;
        if (!this.destroyed) await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
      }
    },
    async newRadio() {
      if (this.busy) return;
      this.selectionError = '';
      this.selectionSlot = null;
      ++this.requestSerial;
      this.switchChoice = null; this.selectedSlot = null; this.pendingSong = null; ++this.switchSerial;
      this.busy = true;
      try {
        const state = await this.json('stop', {method: 'POST', signal: AbortSignal.timeout(8000)});
        if (state?.ok !== true || typeof state.active !== 'boolean') throw new Error('Missing Radio stop receipt');
        if (state.ok === true && state.active === false) {
          this.tuningIntent = null;
          this.saveTuningIntent();
        }
        this.clearLaunch();
        this.applyState(state);
      }
      catch (error) {
        console.debug('[Radio] new radio request interrupted', error);
      } finally {
        this.busy = false;
        await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
      }
    },
    async poll() {
      clearTimeout(this.timer);
      if (this.destroyed || !this.visible) return;
      try { if (!this.busy) await this.refresh(); }
      catch (error) {
        console.debug('[Radio] refresh deferred', error);
        this.loading = false;
      }
      if (!this.destroyed && this.visible) this.timer = setTimeout(() => this.poll(), 2000);
    },
  }, Object.getOwnPropertyDescriptors(radioArtwork()));
}
