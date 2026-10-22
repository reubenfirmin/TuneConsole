/* Home's saved-playlist Radio Session. Playback and additions continue server-side. */
function radioHome(initial = {}) {
  let cached = {};
  try { cached = JSON.parse(sessionStorage.getItem('radio-view') || '{}'); } catch (_) {}
  // Cached artwork is useful; a cached session is not evidence of playback.
  const state = initial.state || {active: false};
  const seeds = initial.seeds?.length ? initial.seeds : cached.seeds || [];
  return {
    state, seeds, visible: false, loading: !seeds.length, busy: false, error: '',
    showSeeds: !state.active, loaded: false, seedsLoading: false, timer: null, playlistKey: '', destroyed: false,
    busyAction: '', seedMenu: initial.seedMenu || cached.seedMenu, seenMenu: null, currentVideo: null, requestSerial: 0,
    livePlayback: initial.playback ? {data: initial.playback, at: Date.now()} : null,
    selectedSlot: null, switchChoice: null, switchSending: false, switchSerial: 0,
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
      this.showSeeds = !state.active;
    },
    playbackChanged(data) {
      this.livePlayback = {data, at: Date.now()};
      const playing = data.now_playing;
      if (this.state.active && !this.state.starting && (!data.connected || !playing || playing.paused ||
          playing.playlist_ytm_id !== this.state.playlist_id)) {
        ++this.requestSerial;
        this.applyState({...this.state, active: false});
        this.cacheView();
        if (!this.loaded) this.loadSeeds();
      }
    },
    init() {
      this.$nextTick(() => {
        if (!this.state.active) return;
        const key = this.tableKey(this.state);
        if (this.$refs.playlist.querySelector('table')) this.playlistKey = key;
        else if (cached.playlistKey === key && cached.playlistHtml) {
          htmx.swap(this.$refs.playlist, cached.playlistHtml, {swapStyle: 'innerHTML'});
          this.playlistKey = key;
        }
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
        if (this.livePlayback && Date.now() - this.livePlayback.at <= 8000)
          this.playbackChanged(this.livePlayback.data);
        else if (this.state.active && !this.state.starting) this.applyState({...this.state, active: false});
        this.$nextTick(() => this.fitSpace()); this.poll();
      }
      else { clearTimeout(this.timer); this.timer = null; }
    },
    fitSpace() {
      if (!this.visible) return;
      for (const [element, variable] of [[this.$refs.seeds, '--radio-grid-height'],
                                        [this.$refs.session, '--radio-session-height']]) {
        if (!element?.getClientRects().length) continue;
        const top = element.getBoundingClientRect().top + window.scrollY;
        element.style.setProperty(variable, Math.max(320, window.innerHeight - top - 20) + 'px');
      }
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
        htmx.swap(this.$refs.playlist, html, {swapStyle: 'innerHTML'});
        this.playlistKey = key;
      }
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
          if (on && current !== this.currentVideo) {
            const scroller = this.$refs.playlist.querySelector('.radio-track-scroll');
            const bounds = scroller?.getBoundingClientRect();
            const rect = row.getBoundingClientRect();
            const header = scroller?.querySelector('thead')?.offsetHeight || 0;
            if (bounds && (rect.top < bounds.top + header || rect.bottom > bounds.bottom))
              scroller.scrollTop += rect.top - bounds.top - header;
          }
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
    async start(song) {
      if (this.busy) return;
      this.busy = true; this.busyAction = 'start'; this.error = '';
      const now = new Date();
      const localDate = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'),
                         String(now.getDate()).padStart(2, '0')].join('-');
      try {
        const result = await this.json('start', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({video_id: song.video_id, local_date: localDate, menu_id: this.seedMenu})});
        if (!result.ok) throw new Error(result.reason || 'Could not start radio.');
        this.applyState(result);
      } catch (error) { this.error = error.message; }
      finally { this.busy = false; this.busyAction = ''; await this.refresh().catch(error => { this.error = error.message; }); }
    },
    select(branch) {
      if (this.busy || branch.active || !this.state.active) return;
      ++this.requestSerial;
      this.error = '';
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
    async stop() {
      this.switchChoice = null; this.selectedSlot = null; ++this.switchSerial;
      this.busy = true;
      try { await this.json('stop', {method: 'POST'}); }
      catch (error) { this.error = error.message; }
      finally { this.busy = false; await this.refresh().catch(error => { this.error = error.message; }); }
    },
    async poll() {
      clearTimeout(this.timer);
      if (this.destroyed || !this.visible) return;
      try { if (!this.busy) await this.refresh(); }
      catch (error) { this.error = error.message; this.loading = false; }
      if (!this.destroyed && this.visible) this.timer = setTimeout(() => this.poll(), 2000);
    },
  };
}
