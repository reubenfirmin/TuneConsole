/* Home's saved-playlist Radio Session. Playback and additions continue server-side. */
function radioHome(initial = {}) {
  let cached = {};
  let pendingLaunch = null;
  let selectionIntent = null;
  let tuningIntent = null;
  let coverLoad = null;
  try { cached = JSON.parse(sessionStorage.getItem('radio-view') || '{}'); } catch (_) {}
  try {
    const saved = JSON.parse(sessionStorage.getItem('radio-launch'));
    if (saved?.version === 1 && (typeof saved.id === 'string' || (saved.id === null && saved.cancelling)) && saved.song &&
        (saved.fromPlaylist ? Number.isInteger(saved.song.id) : typeof saved.song.video_id === 'string')) pendingLaunch = saved;
  } catch (_) {}
  try {
    const saved = JSON.parse(sessionStorage.getItem('radio-tuning'));
    if (saved?.version === 1 && typeof saved.playlistId === 'string' && saved.values && Object.keys(saved.values).length === 3 &&
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
    loaded: !!(initial.seeds?.length && initial.seedMenu), seedsLoading: false, timer: null, playlistKey: '', playlistSessionId: null, destroyed: false,
    preparingKey: '',
    busyAction: '', seedMenu: initial.seedMenu || cached.seedMenu, seenMenu: null, currentVideo: null, requestSerial: 0,
    recordPlayback: null, pendingRecord: null, recordCoverUrl: '',
    launchSong: pendingLaunch?.song || initial.launchPlaylist || (state.active && state.starting ? state.tracks?.[0] || {} : null),
    launchPlaylist: pendingLaunch ? (pendingLaunch.fromPlaylist ? pendingLaunch.song : null) : initial.launchPlaylist || null,
    launchPreviousId: pendingLaunch?.previousId ?? null,
    launchAttempt: pendingLaunch?.id || null,
    launchPhase: pendingLaunch ? (pendingLaunch.cancelling ? 'cancel-unknown' : 'unknown') : '',
    launchSerial: 0, launchCancelling: !!pendingLaunch?.cancelling, cancelConfirmed: false, cancelSending: false,
    recoverySong: pendingLaunch?.song || null,
    recoveryFromPlaylist: pendingLaunch?.fromPlaylist || false, launchRecoveryTimer: null,
    selectedSlot: null, switchChoice: null, switchSending: false, switchSerial: selectionIntent?.serial || 0,
    selectionIntent, selectionError: '', selectionSlot: selectionIntent?.slot ?? null, pendingSong: null,
    hoveredSlot: null, focusedSlot: null,
    tuningDraft: {comfort: 50, theme: 50, energy: 50}, tuningBusy: false, tuningIntent, tuningSerial: 0,
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
    get displayDirections() {
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
      this.tuningDraft = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Math.round(value * 100)]));
      this.$refs.tuningDialog.showModal();
    },
    resetTuning() { this.tuningDraft = {comfort: 50, theme: 50, energy: 50}; },
    saveTuningIntent() {
      try {
        if (this.tuningIntent) sessionStorage.setItem('radio-tuning', JSON.stringify({version: 1, ...this.tuningIntent}));
        else sessionStorage.removeItem('radio-tuning');
      } catch (_) {}
    },
    get tuningMessage() {
      if (this.tuningBusy) return 'Tuning your future songs…';
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
      this.tuningIntent = intent;
      this.saveTuningIntent();
      const selectionSerial = this.switchSerial;
      ++this.tuningSerial;
      ++this.requestSerial;
      this.tuningBusy = true;
      try {
        const result = await this.json('tune', {method: 'POST', headers: {'Content-Type': 'application/json',
          'X-Radio-Tuning-Client': 'visible-progress-v2'},
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
        moving: this.visible && this.state.active && !this.state.starting && !this.destroyed,
        connectedSlot: this.connectedSlot,
        directionCount: this.state.directions?.length || 4,
      };
    },
    sessionId(state = this.state) { return state.playlist_id || state.local_id; },
    get preparingSession() { return !!(this.state.active && this.sessionId() && this.playlistSessionId !== this.sessionId()); },
    get restoring() { return (this.resolving || this.preparingSession) && !this.launchSong && !this.state.starting && this.busyAction !== 'start'; },
    get launching() { return !!this.launchPhase || this.resolving || this.busyAction === 'start' || !!this.launchSong || (this.state.active && this.state.starting) || this.preparingSession; },
    get launchUncertain() { return this.launchCancelling || ['checking', 'unknown'].includes(this.launchPhase); },
    get recoveryTitle() { return this.recoverySong?.title || 'Your Radio Session'; },
    get recoveryArtist() { return this.recoverySong?.artist || (this.recoveryFromPlaylist ? 'Radio from your playlist' : ''); },
    get recoveryUrl() {
      if (this.recoveryFromPlaylist) return '/playlist/' + this.recoverySong.id;
      return 'https://music.youtube.com/watch?v=' + encodeURIComponent(this.recoverySong?.video_id || '');
    },
    get recoveryMessage() {
      if (this.launchPhase === 'cancelling') return 'Stopping your radio…';
      if (this.launchPhase === 'cancel-unknown') return 'We haven’t confirmed your Stop request. Try Cancel again.';
      if (this.launchPhase === 'checking') return 'Checking whether your session started. This can take up to 30 seconds.';
      if (this.launchPhase === 'unknown') return 'We couldn’t confirm playback. Check again, or play your selection directly on YouTube Music.';
      if (this.launchPhase === 'disconnected') return 'Open YouTube Music and connect its extension, then try this selection again.';
      return 'This Radio Session didn’t start. Try this selection again, or play it directly.';
    },
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
    async prepareSession() {
      await this.$nextTick();
      const state = this.state, sessionId = this.sessionId(state);
      if (this.destroyed || this.resolving || !state.active || state.starting || !sessionId ||
          this.playlistSessionId === sessionId || this.playlistKey !== this.tableKey(state) ||
          !this.playlistMatches(state) || !(state.directions?.length || state.exhausted) ||
          this.$refs.directions.querySelectorAll('.radio-direction').length !== (state.directions || []).length) return;
      const key = JSON.stringify([this.playlistKey, state.directions]);
      if (this.preparingKey === key) return;
      this.preparingKey = key;
      // An established session already has a published queue and streams.
      // Reveal that useful content now; its artwork loads in place. Only a
      // newly requested launch waits for its initial presentation to settle.
      if (!this.launchSong && !this.launchPhase && this.busyAction !== 'start') {
        this.playlistSessionId = sessionId;
        this.fitSpace();
        this.cacheView();
        return;
      }
      // Hidden lazy images and the record label must settle before reveal.
      // Failed or stalled artwork uses the existing record placeholders.
      const elements = Array.from(this.$refs.session.querySelectorAll('img, image:not(.radio-record-cover)'));
      const urls = new Map();
      for (const element of elements) {
        if (element.tagName === 'IMG') element.loading = 'eager';
        const url = element.getAttribute('src') || element.getAttribute('href');
        if (!url) continue;
        if (!urls.has(url)) urls.set(url, []);
        urls.get(url).push(element);
      }
      const record = this.$refs.session.querySelector('.radio-record-cover');
      await Promise.all([record ? this.syncRecordCover() : Promise.resolve(),
        ...Array.from(urls, ([url, images]) => new Promise(resolve => {
        const image = new Image();
        let timer, settled = false;
        const fallback = element => {
          if (this.destroyed || this.preparingKey !== key) return;
          if ((element.getAttribute('src') || element.getAttribute('href')) !== url) return;
          element.remove();
        };
        const finish = ok => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          image.onload = image.onerror = null;
          if (!ok) images.forEach(fallback);
          resolve();
        };
        image.referrerPolicy = 'no-referrer';
        image.onload = () => Promise.all(images.filter(element => element.tagName === 'IMG').map(element =>
          element.decode().catch(() => fallback(element)))).then(() => finish(true));
        image.onerror = () => finish(false);
        timer = setTimeout(() => finish(false), 8000);
        image.src = url;
      }))]);
      if (this.destroyed || this.preparingKey !== key || !this.state.active || this.resolving ||
          this.state.starting || this.sessionId() !== sessionId ||
          JSON.stringify([this.playlistKey, this.state.directions]) !== key) return;
      this.fitSpace();
      this.playlistSessionId = sessionId;
      this.finishLaunch();
      this.cacheView();
      this.$nextTick(() => this.fitSpace());
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
          (sessionId === tuning.playlistId &&
            Object.keys(tuning.values).every(key => state.tuning?.[key] === tuning.values[key])))) {
        this.tuningIntent = null;
        this.saveTuningIntent();
        this.$refs.tuningDialog?.close();
      }
      if (!state.active || state.playlist_id !== this.state.playlist_id) {
        this.selectedSlot = null;
        this.playlistSessionId = null;
        this.preparingKey = '';
        this.currentVideo = null;
        this.recordPlayback = null;
        this.pendingRecord = null;
        this.pendingSong = null;
        this.selectionSlot = null;
        this.selectionError = '';
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
      this.resolving = !!state.resolving;
      this.publishSessionState();
      this.syncArtworkMotion();
      this.animateQueues(changed.filter(slot => slot !== this.pendingSong?.slot));
      if (state.active && state.starting && !this.launchSong) this.launchSong = state.tracks?.[0] || {};
    },
    publishSessionState() {
      this.$dispatch('radio-session-state', {active: !!this.state.active, starting: !!this.state.starting,
        playlist: this.state.playlist_id || null});
    },
    finishLaunch() {
      if (this.launchCancelling) {
        // An accepted Stop still has to drain an already admitted remote write.
        // Neither a missing receipt nor an older inactive snapshot settles it.
        const identified = this.launchAttempt && Array.isArray(this.state.stopping_launches);
        const drained = identified ? !this.state.stopping_launches.includes(this.launchAttempt) &&
          (this.state.launch_id !== this.launchAttempt || this.state.stopped || (!this.state.active && !this.state.starting)) :
          !this.state.active && !this.state.starting && !this.state.stopping;
        if (this.cancelConfirmed && !this.cancelSending && drained) {
          this.launchCancelling = false;
          this.cancelConfirmed = false;
          this.launchPhase = '';
          clearTimeout(this.launchRecoveryTimer);
          this.launchSong = null;
          this.launchPlaylist = null;
          this.clearLaunch();
          this.tuningIntent = null;
          this.saveTuningIntent();
        }
        return;
      }
      if (!this.launchSong || this.busyAction === 'start') return;
      // An inactive or previous-session snapshot cannot settle a lost POST.
      // Keep its identity and block another create until new playback is known.
      if (this.launchUncertain && (!this.state.active || this.sessionId() === this.launchPreviousId)) return;
      if (!this.state.active) {
        console.debug('[Radio] launch returned to song choices', this.state.error);
        this.launchPhase = 'rejected';
        this.launchSong = null;
        this.launchPlaylist = null;
        this.clearLaunch();
      } else if (!this.state.starting && this.playlistSessionId === this.sessionId() &&
                 this.playlistKey === this.tableKey(this.state)) {
        this.launchPhase = '';
        clearTimeout(this.launchRecoveryTimer);
        this.launchSong = null;
        this.launchPlaylist = null;
        this.clearLaunch();
      }
    },
    rememberLaunch() {
      try { sessionStorage.setItem('radio-launch', JSON.stringify({version: 1,
        id: this.launchAttempt, song: this.recoverySong, fromPlaylist: this.recoveryFromPlaylist,
        previousId: this.launchPreviousId, cancelling: this.launchCancelling})); } catch (_) {}
    },
    clearLaunch() {
      try {
        const saved = JSON.parse(sessionStorage.getItem('radio-launch'));
        if (saved?.id === this.launchAttempt) sessionStorage.removeItem('radio-launch');
      } catch (_) {}
      this.launchAttempt = null;
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
      if (this.launchPlaylist && !this.launchUncertain) {
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
        this.fitSpace();
        this.prepareSession();
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
      if (this.seedsLoading || this.loaded) return;
      this.seedsLoading = true;
      try {
        const offer = await this.json('seeds');
        if (this.destroyed) return;
        this.seeds = offer.tracks; this.seedMenu = offer.menu_id; this.loaded = true;
        this.cacheView();
        this.$nextTick(() => { this.fitSpace(); document.dispatchEvent(new Event('listening-options')); });
      } catch (error) { console.debug('[Radio] seed refresh deferred', error); }
      finally { this.seedsLoading = false; this.loading = false; }
    },
    setVisible(value) {
      if (this.visible === value) return;
      this.visible = value;
      this.syncArtworkMotion();
      if (value) {
        this.$nextTick(() => { this.fitSpace(); document.dispatchEvent(new Event('listening-options')); }); this.poll();
      }
      else { clearTimeout(this.timer); this.timer = null; }
    },
    destroy() {
      this.destroyed = true;
      clearTimeout(this.timer);
      clearTimeout(this.launchRecoveryTimer);
      coverLoad?.cancel();
      this.destroyArtwork();
    },
    async json(path, options) {
      const response = await fetch('/radio/' + path, ['status', 'select', 'tune'].includes(path)
        ? {signal: AbortSignal.timeout(8000), ...options} : options);
      if (!response.ok) throw new Error('Could not reach Radio. Try again.');
      return response.json();
    },
    async refresh() {
      const serial = ++this.requestSerial;
      const state = await this.json('status');
      if (this.destroyed || serial !== this.requestSerial || this.busy || this.tuningBusy) return;
      this.applyState(state);
      if (!this.loaded && !state.active) this.loadSeeds();
      this.loading = false;
      const key = this.tableKey(state);
      if (!this.launchCancelling && state.active && this.sessionId(state) && key !== this.playlistKey) {
        const response = await fetch('/radio/playlist');
        if (!response.ok) throw new Error('Could not load the session playlist.');
        const html = await response.text();
        if (this.destroyed || serial !== this.requestSerial || this.busy || this.tuningBusy) return;
        const content = document.createElement('div');
        content.innerHTML = html;
        if (!this.playlistMatches(state, content)) throw new Error('Radio history is still loading.');
        htmx.swap(this.$refs.playlist, html, {swapStyle: 'innerHTML'},
          {eventInfo: {target: this.$refs.playlist}});
        this.playlistKey = key;
      }
      this.finishLaunch();
      this.cacheView();
      this.$nextTick(() => {
        this.fitSpace();
        this.prepareSession();
        const current = state.browser_age != null && state.browser_age <= 8 && state.browser?.player?.playlist_ytm_id === state.playlist_id ?
          state.browser.player.video_id : null;
        this.$refs.playlist.querySelectorAll('tr[data-vid]').forEach(row => {
          const on = row.dataset.vid === current;
          row.classList.toggle('radio-playing', on);
          if (on) row.setAttribute('aria-current', 'true');
          else row.removeAttribute('aria-current');
        });
        if (current) this.currentVideo = current;
        if (this.visible && state.active && state.menu_id && this.seenMenu !== state.menu_id) {
          const menuId = state.menu_id;
          this.seenMenu = menuId;
          this.json('seen', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({menu_id: menuId})}).catch(() => {
              if (this.seenMenu === menuId) this.seenMenu = null;
            });
        }
      });
    },
    async start(song, fromPlaylist = false) {
      if (this.busy || this.launchUncertain || this.cancelSending) return;
      const serial = ++this.launchSerial;
      window.tvPlaybackControls?.release();
      ++this.requestSerial;
      this.launchPreviousId = this.sessionId() ?? null;
      this.launchPlaylist = fromPlaylist ? song : null;
      this.launchSong = song;
      this.launchPhase = '';
      this.recoverySong = {...song}; this.recoveryFromPlaylist = fromPlaylist;
      this.launchAttempt = crypto.randomUUID();
      const launchId = this.launchAttempt;
      // Persist before sending. Reloading an admitted request with a missing
      // receipt must reconcile it, never silently permit another creation.
      this.rememberLaunch();
      clearTimeout(this.launchRecoveryTimer);
      this.busy = true; this.busyAction = 'start';
      this.$nextTick(() => this.fitSpace());
      const now = new Date();
      const localDate = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'),
                         String(now.getDate()).padStart(2, '0')].join('-');
      try {
        const result = await this.json('start', {method: 'POST', signal: AbortSignal.timeout(45000), headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({...(fromPlaylist ? {playlist_id: song.id} : {video_id: song.video_id, menu_id: this.seedMenu}),
                                local_date: localDate, launch_id: launchId})});
        if (this.destroyed || serial !== this.launchSerial) return;
        if (!result.ok) {
          console.debug('[Radio] launch returned to song choices', result.reason);
          this.launchPhase = result.reason === 'Connect the YouTube Music extension first.' ? 'disconnected' : 'rejected';
          this.launchSong = null;
          this.launchPlaylist = null;
          this.clearLaunch();
          if (result.refresh_seeds) { this.loaded = false; this.loadSeeds(); }
          return;
        }
        this.applyState(result);
      } catch (error) {
        if (this.destroyed || serial !== this.launchSerial) return;
        console.debug('[Radio] launch request interrupted', error);
        this.launchPhase = 'checking';
        this.launchRecoveryTimer = setTimeout(() => {
          if (this.destroyed || this.launchPhase !== 'checking') return;
          this.launchPhase = 'unknown';
          this.$nextTick(() => this.fitSpace());
        }, 30000);
        // The server may have accepted the launch before the response was
        // lost. Let status polling settle it without issuing another start.
      } finally {
        if (!this.destroyed && serial === this.launchSerial) {
          this.busy = false;
          this.busyAction = '';
          await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
        }
      }
    },
    async cancelLaunch() {
      if (this.destroyed || this.cancelSending) return;
      const serial = ++this.launchSerial;
      ++this.requestSerial;
      this.launchCancelling = true;
      this.launchPhase = 'cancelling';
      this.busy = false;
      this.busyAction = '';
      this.cancelSending = true;
      this.launchAttempt ||= this.state.launch_id || null;
      this.recoverySong ||= this.launchSong || this.state.tracks?.[0] || {};
      this.rememberLaunch();
      clearTimeout(this.launchRecoveryTimer);
      this.launchRecoveryTimer = setTimeout(() => {
        if (this.destroyed || !this.launchCancelling || serial !== this.launchSerial) return;
        this.launchPhase = 'cancel-unknown';
      }, 30000);
      try {
        const state = await this.json('stop', {method: 'POST', signal: AbortSignal.timeout(8000),
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify({launch_id: this.launchAttempt})});
        if (this.destroyed || serial !== this.launchSerial) return;
        if (state.ok !== true) throw new Error('Missing Radio stop receipt');
        this.cancelConfirmed = true;
        this.cancelSending = false;
        this.applyState(state);
        this.finishLaunch();
        this.cacheView();
        this.$nextTick(() => this.fitSpace());
      } catch (error) {
        if (!this.destroyed && serial === this.launchSerial) this.launchPhase = 'cancel-unknown';
        console.debug('[Radio] launch stop deferred', error);
      } finally {
        if (!this.destroyed && serial === this.launchSerial) {
          this.cancelSending = false;
          await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
        }
      }
    },
    select(branch, song = null) {
      if (this.busy || (!song && branch.active) || !this.state.active) return;
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
      if (this.busy || this.launchUncertain) return;
      this.selectionError = '';
      this.selectionSlot = null;
      ++this.requestSerial;
      this.switchChoice = null; this.selectedSlot = null; this.pendingSong = null; ++this.switchSerial;
      this.busy = true;
      try {
        const state = await this.json('stop', {method: 'POST'});
        if (state.ok === true && state.active === false) {
          this.tuningIntent = null;
          this.saveTuningIntent();
        }
        this.launchSong = null;
        this.launchPlaylist = null;
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
