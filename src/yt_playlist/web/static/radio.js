/* Home's saved-playlist Radio Session. Playback and additions continue server-side. */
function radioHome(initial = {}) {
  let cached = {};
  try { cached = JSON.parse(sessionStorage.getItem('radio-view') || '{}'); } catch (_) {}
  // Cached artwork is useful; a cached session is not evidence of playback.
  const state = initial.state || {active: false};
  const seeds = initial.seeds?.length ? initial.seeds : cached.seeds || [];
  return Object.defineProperties({
    state, seeds, visible: false, loading: !seeds.length, busy: false, resolving: !!state.resolving,
    loaded: false, seedsLoading: false, timer: null, playlistKey: '', playlistSessionId: null, destroyed: false,
    preparingKey: '',
    busyAction: '', seedMenu: initial.seedMenu || cached.seedMenu, seenMenu: null, currentVideo: null, requestSerial: 0,
    recordPlayback: null, pendingRecord: null, recordCoverFailed: '',
    launchSong: initial.launchPlaylist || (state.active && state.starting ? state.tracks?.[0] || {} : null),
    launchPlaylist: initial.launchPlaylist || null,
    selectedSlot: null, switchChoice: null, switchSending: false, switchSerial: 0,
    hoveredSlot: null, focusedSlot: null,
    tuningDraft: {comfort: 50, theme: 50, energy: 50}, tuningBusy: false,
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
    },
    openTuning() {
      if (this.tuningBusy) return;
      const values = this.state.tuning || {comfort: .5, theme: .5, energy: .5};
      this.tuningDraft = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Math.round(value * 100)]));
      this.$refs.tuningDialog.showModal();
    },
    resetTuning() { this.tuningDraft = {comfort: 50, theme: 50, energy: 50}; },
    async applyTuning() {
      if (this.tuningBusy) return;
      this.tuningBusy = true;
      this.$refs.tuningDialog.close();
      try {
        const result = await this.json('tune', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(Object.fromEntries(Object.entries(this.tuningDraft).map(([key, value]) => [key, value / 100])))});
        if (this.destroyed) return;
        if (result.ok) this.applyState(result);
      } catch (error) { console.debug('[Radio] tuning deferred', error); }
      finally { this.tuningBusy = false; }
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
    get preparingSession() { return !!(this.state.active && this.state.local_id && this.playlistSessionId !== this.state.local_id); },
    get restoring() { return (this.resolving || this.preparingSession) && !this.launchSong && !this.state.starting && this.busyAction !== 'start'; },
    get launching() { return this.resolving || this.busyAction === 'start' || !!this.launchSong || (this.state.active && this.state.starting) || this.preparingSession; },
    get showSeeds() { return !this.state.active && !this.launching; },
    get launchTracks() {
      const tracks = [this.launchSong, ...(this.launchPlaylist?.tracks || []),
        ...(this.state.tracks || []).slice(0, 8), ...this.seeds].filter(song => song?.video_id);
      return tracks.filter((song, index) => tracks.findIndex(t => t.video_id === song.video_id) === index).slice(0, 8);
    },
    historyIds(state) {
      return (state.history || (state.tracks || []).slice(0, state.history_count || 0)).map(t => t.video_id);
    },
    tableKey(state) {
      return JSON.stringify([state.local_id, state.metadata_revision || 0, this.historyIds(state)]);
    },
    playlistMatches(state, root = this.$refs.playlist) {
      return !!root?.querySelector('table') && JSON.stringify(
        Array.from(root.querySelectorAll('tr[data-vid]'), row => row.dataset.vid)
      ) === JSON.stringify(this.historyIds(state));
    },
    async prepareSession() {
      await this.$nextTick();
      const state = this.state, sessionId = state.local_id;
      if (this.destroyed || this.resolving || !state.active || state.starting || !sessionId ||
          this.playlistSessionId === sessionId || this.playlistKey !== this.tableKey(state) ||
          !this.playlistMatches(state) || !(state.directions?.length || state.exhausted) ||
          this.$refs.directions.querySelectorAll('.radio-direction').length !== (state.directions || []).length) return;
      const key = JSON.stringify([this.playlistKey, state.directions]);
      if (this.preparingKey === key) return;
      this.preparingKey = key;
      // Hidden lazy images and the record label must settle before reveal.
      // Failed or stalled artwork uses the existing record placeholders.
      const elements = Array.from(this.$refs.session.querySelectorAll('img, image'));
      const urls = new Map();
      for (const element of elements) {
        if (element.tagName === 'IMG') element.loading = 'eager';
        const url = element.getAttribute('src') || element.getAttribute('href');
        if (!url) continue;
        if (!urls.has(url)) urls.set(url, []);
        urls.get(url).push(element);
      }
      await Promise.all(Array.from(urls, ([url, images]) => new Promise(resolve => {
        const image = new Image();
        let timer, settled = false;
        const fallback = element => {
          if (this.destroyed || this.preparingKey !== key) return;
          if ((element.getAttribute('src') || element.getAttribute('href')) !== url) return;
          // The playing cover stays mounted so a later song can replace it.
          if (element.matches('.radio-record-cover')) this.recordCoverFailed = url;
          else element.remove();
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
      })));
      if (this.destroyed || this.preparingKey !== key || !this.state.active || this.resolving ||
          this.state.starting || this.state.local_id !== sessionId ||
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
      if (!state.active || state.playlist_id !== this.state.playlist_id) {
        this.selectedSlot = null;
        this.playlistSessionId = null;
        this.preparingKey = '';
        this.currentVideo = null;
        this.recordPlayback = null;
        this.pendingRecord = null;
        this.recordCoverFailed = '';
      }
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
      this.animateQueues(changed);
      if (state.active && state.starting && !this.launchSong) this.launchSong = state.tracks?.[0] || {};
    },
    publishSessionState() {
      this.$dispatch('radio-session-state', {active: !!this.state.active, starting: !!this.state.starting});
    },
    finishLaunch() {
      if (!this.launchSong || this.busyAction === 'start') return;
      if (!this.state.active) {
        console.debug('[Radio] launch returned to song choices', this.state.error);
        this.launchSong = null;
        this.launchPlaylist = null;
      } else if (!this.state.starting && this.playlistSessionId === this.state.local_id &&
                 this.playlistKey === this.tableKey(this.state)) {
        this.launchSong = null;
        this.launchPlaylist = null;
      }
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
        this.fitSpace();
        this.prepareSession();
      });
      if (!this.state.active) this.loadSeeds();
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
      this.destroyArtwork();
    },
    async json(path, options) {
      const response = await fetch('/radio/' + path, options);
      if (!response.ok) throw new Error('Could not reach Radio. Try again.');
      return response.json();
    },
    async refresh() {
      const serial = ++this.requestSerial;
      const state = await this.json('status');
      if (this.destroyed || serial !== this.requestSerial || this.busy) return;
      this.applyState(state);
      if (!this.loaded && !state.active) this.loadSeeds();
      this.loading = false;
      const key = this.tableKey(state);
      if (state.active && state.local_id && key !== this.playlistKey) {
        const response = await fetch('/radio/playlist');
        if (!response.ok) throw new Error('Could not load the session playlist.');
        const html = await response.text();
        if (this.destroyed || serial !== this.requestSerial || this.busy) return;
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
      if (this.busy) return;
      ++this.requestSerial;
      this.launchPlaylist = fromPlaylist ? song : null;
      this.launchSong = song;
      this.busy = true; this.busyAction = 'start';
      this.$nextTick(() => this.fitSpace());
      const now = new Date();
      const localDate = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'),
                         String(now.getDate()).padStart(2, '0')].join('-');
      try {
        const result = await this.json('start', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({...(fromPlaylist ? {playlist_id: song.id} : {video_id: song.video_id, menu_id: this.seedMenu}),
                                local_date: localDate})});
        if (!result.ok) {
          console.debug('[Radio] launch returned to song choices', result.reason);
          this.launchSong = null;
          this.launchPlaylist = null;
          if (result.refresh_seeds) { this.loaded = false; this.loadSeeds(); }
          return;
        }
        this.applyState(result);
      } catch (error) {
        console.debug('[Radio] launch request interrupted', error);
        // The server may have accepted the launch before the response was
        // lost. Let status polling settle it without issuing another start.
      } finally {
        this.busy = false;
        this.busyAction = '';
        await this.refresh().catch(error => console.debug('[Radio] refresh deferred', error));
      }
    },
    select(branch) {
      if (this.busy || branch.active || !this.state.active) return;
      ++this.requestSerial;
      this.selectedSlot = branch.slot;
      this.switchChoice = {proposal_id: branch.id, menu_id: this.state.menu_id,
        serial: ++this.switchSerial};
      this.applyState({...this.state, pending_slot: branch.slot, updating: true});
      this.cacheView();
      this.sendSelection();
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
            const result = await this.json('select', {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({proposal_id: choice.proposal_id, menu_id: choice.menu_id})});
            if (this.destroyed || choice.serial !== this.switchSerial) continue;
            if (!result.ok) throw new Error(result.reason || 'Could not switch radio direction.');
            this.applyState(result);
            this.cacheView();
          } catch (error) {
            if (choice.serial === this.switchSerial) this.selectedSlot = null;
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
      ++this.requestSerial;
      this.switchChoice = null; this.selectedSlot = null; ++this.switchSerial;
      this.busy = true;
      try {
        const state = await this.json('stop', {method: 'POST'});
        this.launchSong = null;
        this.launchPlaylist = null;
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
