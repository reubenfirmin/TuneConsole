/* Home's saved-playlist Radio Session. Playback and additions continue server-side. */
function radioHome(initial = {}) {
  let cached = {};
  try { cached = JSON.parse(sessionStorage.getItem('radio-view') || '{}'); } catch (_) {}
  // Cached artwork is useful; a cached session is not evidence of playback.
  const state = initial.state || {active: false};
  const seeds = initial.seeds?.length ? initial.seeds : cached.seeds || [];
  return {
    state, seeds, visible: false, loading: !seeds.length, busy: false,
    loaded: false, seedsLoading: false, timer: null, playlistKey: '', destroyed: false,
    busyAction: '', seedMenu: initial.seedMenu || cached.seedMenu, seenMenu: null, currentVideo: null, requestSerial: 0,
    launchSong: initial.launchPlaylist || (state.active && state.starting ? state.tracks?.[0] || {} : null),
    launchPlaylist: initial.launchPlaylist || null,
    selectedSlot: null, switchChoice: null, switchSending: false, switchSerial: 0,
    hoveredSlot: null, focusedSlot: null, queueCenters: {},
    get activeSlot() { return this.state.directions?.find(branch => branch.active)?.slot ?? 0; },
    get previewSlot() { return this.hoveredSlot ?? this.focusedSlot; },
    get connectedSlot() { return this.previewSlot ?? this.activeSlot; },
    queueColor(slot) { return `var(--radio-queue-${slot % 4})`; },
    measureQueues() {
      const bounds = this.$refs.junction?.getBoundingClientRect();
      if (!bounds?.width) return;
      const centers = {};
      this.$refs.directions?.querySelectorAll('.radio-direction').forEach(queue => {
        const rect = queue.getBoundingClientRect();
        centers[queue.dataset.slot] = (rect.left + rect.width / 2 - bounds.left) / bounds.width * 1000;
      });
      this.queueCenters = centers;
    },
    junctionStyle(slot, detail = false) {
      const x = Math.round(this.queueCenters[slot] ?? (slot + .5) * 1000 / (this.state.directions?.length || 4));
      const left = Math.max(12, Math.min(180, x - 85));
      const right = Math.min(988, Math.max(820, x + 85));
      // Two broad, tangent-continuous arms join a single stem. Keep every
      // destination's commands identical so the whole drawing can morph.
      const path = detail
        ? `M${left + 12} 0 C${left + 12} 36 ${x - 6} 26 ${x - 6} 82 L${x - 6} 112 M${right - 12} 0 C${right - 12} 36 ${x + 6} 26 ${x + 6} 82 L${x + 6} 112`
        : `M${left} 0 C${left} 48 ${x - 18} 22 ${x - 18} 82 L${x - 18} 112 L${x + 18} 112 L${x + 18} 82 C${x + 18} 22 ${right} 48 ${right} 0 L${right - 24} 0 C${right - 24} 26 ${x} 22 ${x} 68 C${x} 22 ${left + 24} 26 ${left + 24} 0 Z`;
      return `d: path("${path}")`;
    },
    layoutHistory() {
      const scroller = this.$refs.playlist?.querySelector('.radio-track-scroll');
      const space = scroller?.querySelector('.radio-history-space');
      const history = scroller?.querySelector('.radio-history');
      if (!space || !history || !scroller.clientHeight) return;
      const headerHeight = scroller.querySelector('thead')?.getBoundingClientRect().height || 0;
      const rowHeight = history.querySelector('tr')?.getBoundingClientRect().height || 48;
      const height = Math.max(0, scroller.clientHeight - headerHeight - history.getBoundingClientRect().height);
      scroller.style.setProperty('--radio-space-height', height + 'px');
      scroller.style.setProperty('--radio-table-head', headerHeight + 'px');
      const layoutKey = [scroller.clientWidth, scroller.clientHeight, height, history.lastElementChild?.dataset.vid].join(':');
      if (space.dataset.layout === layoutKey) return;
      space.dataset.layout = layoutKey;
      const count = Math.ceil(height / rowHeight);
      space.replaceChildren(...Array.from({length: count}, () => {
        const row = document.createElement('tr');
        row.className = 'radio-empty-row';
        row.style.height = height / count + 'px';
        const cell = row.insertCell();
        cell.colSpan = scroller.querySelector('thead tr')?.children.length || 10;
        return row;
      }));
      // Keep the newest played row at the junction, including after a table swap.
      scroller.scrollTop = scroller.scrollHeight;
    },
    get launching() { return this.busyAction === 'start' || !!this.launchSong || (this.state.active && this.state.starting); },
    get showSeeds() { return !this.state.active && !this.launching; },
    get launchTracks() {
      const tracks = [this.launchSong, ...(this.launchPlaylist?.tracks || []),
        ...(this.state.tracks || []).slice(0, 8), ...this.seeds].filter(song => song?.video_id);
      return tracks.filter((song, index) => tracks.findIndex(t => t.video_id === song.video_id) === index).slice(0, 8);
    },
    launchPosition(index) {
      const angle = index * Math.PI / 4;
      return {x: 50 + 38 * Math.cos(angle), y: 50 + 34 * Math.sin(angle)};
    },
    tableKey(state) {
      return JSON.stringify([state.local_id, (state.tracks || []).slice(0, state.history_count || 0).map(t => t.video_id)]);
    },
    applyState(state) {
      if (!state.active || state.playlist_id !== this.state.playlist_id) this.selectedSlot = null;
      if (this.selectedSlot != null) {
        const slot = this.selectedSlot;
        const acknowledged = state.pending_slot === slot || (!state.updating &&
          (state.directions || []).some(branch => branch.slot === slot && branch.active));
        if (acknowledged && !this.switchChoice && !this.switchSending) this.selectedSlot = null;
        else state = {...state, directions: (state.directions || []).map(branch =>
          ({...branch, active: branch.slot === slot}))};
      }
      this.state = state;
      if (state.active && state.starting && !this.launchSong) this.launchSong = state.tracks?.[0] || {};
    },
    finishLaunch() {
      if (!this.launchSong || this.busyAction === 'start') return;
      if (!this.state.active) {
        console.debug('[Radio] launch returned to song choices', this.state.error);
        this.launchSong = null;
        this.launchPlaylist = null;
      } else if (!this.state.starting && this.playlistKey === this.tableKey(this.state)) {
        this.launchSong = null;
        this.launchPlaylist = null;
      }
    },
    init() {
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
        if (this.$refs.playlist.querySelector('table')) this.playlistKey = key;
        else if (cached.playlistKey === key && cached.playlistHtml) {
          htmx.swap(this.$refs.playlist, cached.playlistHtml, {swapStyle: 'innerHTML'},
            {eventInfo: {target: this.$refs.playlist}});
          this.playlistKey = key;
        }
        this.$refs.playlist.querySelector('.radio-history-space')?.removeAttribute('data-layout');
        this.fitSpace();
      });
      if (!this.state.active) this.loadSeeds();
    },
    cacheView() {
      try { sessionStorage.setItem('radio-view', JSON.stringify({seeds: this.seeds,
        seedMenu: this.seedMenu, playlistKey: this.playlistKey, playlistHtml: this.$refs.playlist.innerHTML})); }
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
        this.$nextTick(() => this.fitSpace());
      } catch (error) { console.debug('[Radio] seed refresh deferred', error); }
      finally { this.seedsLoading = false; this.loading = false; }
    },
    setVisible(value) {
      if (this.visible === value) return;
      this.visible = value;
      if (value) {
        this.$nextTick(() => this.fitSpace()); this.poll();
      }
      else { clearTimeout(this.timer); this.timer = null; }
    },
    fitSpace() {
      if (!this.visible) return;
      for (const [element, variable] of [[this.$refs.seeds, '--radio-grid-height'],
                                        [this.$refs.launch, '--radio-launch-height'],
                                        [this.$refs.session, '--radio-session-height']]) {
        if (!element?.getClientRects().length) continue;
        const top = element.getBoundingClientRect().top + window.scrollY;
        const height = Math.max(320, window.innerHeight - top - 20);
        element.style.setProperty(variable, height + 'px');
        if (element === this.$refs.session)
          element.style.setProperty('--radio-history-height', Math.max(140, Math.min(height * .52, window.innerHeight * .5 - top - 16)) + 'px');
      }
      this.layoutHistory();
      this.measureQueues();
    },
    destroy() { this.destroyed = true; clearTimeout(this.timer); },
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
        htmx.swap(this.$refs.playlist, html, {swapStyle: 'innerHTML'},
          {eventInfo: {target: this.$refs.playlist}});
        this.playlistKey = key;
      }
      this.finishLaunch();
      this.cacheView();
      this.$nextTick(() => {
        this.fitSpace();
        const current = state.browser_age != null && state.browser_age <= 8 && state.browser?.player?.playlist_ytm_id === state.playlist_id ?
          state.browser.player.video_id : null;
        this.$refs.playlist.querySelectorAll('tr[data-vid]').forEach(row => {
          const on = row.dataset.vid === current;
          row.classList.toggle('radio-playing', on);
          if (on) row.setAttribute('aria-current', 'true');
          else row.removeAttribute('aria-current');
        });
        this.currentVideo = current;
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
  };
}
