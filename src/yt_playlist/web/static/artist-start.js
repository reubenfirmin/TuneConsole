function artistStart(initial) {
  return {
    selected: initial.selected, songs: initial.songs || [], minimum: initial.minimum, related: [],
    stage: 'artists', saving: false, error: '', destroyed: false,
    relatedNote: '', relatedLoading: false,
    expanded: false, spotifySteps: false, pendingPlaylist: null, relatedVersion: 0, resuming: false,
    get artistChoices() {
      const rows = [...this.selected, ...this.related];
      const seen = new Set();
      return rows.filter(row => { if (seen.has(row.browse_id)) return false; seen.add(row.browse_id); return true; });
    },
    get artistLimit() { return Math.max(6, this.selected.length + 3); },
    get hasMoreArtists() { return this.artistChoices.length > this.artistLimit; },
    get visibleArtists() { return this.artistChoices.slice(0, this.expanded ? this.artistChoices.length : this.artistLimit); },
    isSelected(item) { return this.selected.some(row => row.browse_id === item.browse_id); },
    applyPicks(state) {
      this.selected = state.selected;
      if (Array.isArray(state.saved_songs)) this.songs = state.saved_songs;
      this.suggestions();
    },
    async refreshSongs() {
      try {
        const response = await fetch('/home/onboard/artists/state', {signal: AbortSignal.timeout(20000)});
        if (response.ok && !this.destroyed) this.applyPicks(await response.json());
      } catch (error) { console.debug('[Music setup] saved picks read unavailable', error); }
    },
    clearSearch() {
      document.getElementById('artist-start-search').focus();
    },
    async toggle(item) {
      if (this.saving) return;
      const selecting = !this.isSelected(item);
      this.saving = true; this.error = '';
      try {
        const response = await fetch('/home/onboard/artists/select', {
          method: 'POST', signal: AbortSignal.timeout(20000), headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({browse_id: item.browse_id, selected: selecting})});
        if (!response.ok) {
          console.debug('[Artist setup] choice response unavailable', response.status, await response.text());
          throw new Error('Artist choice failed');
        }
        const body = await response.json();
        if (!Array.isArray(body.selected)) throw new Error('Artist choice response unavailable');
        if (this.destroyed) return;
        this.selected = body.selected;
        if (selecting) {
          this.related = Array.isArray(body.related) ? body.related : [];
          this.suggestions();
          this.clearSearch();
        } else {
          this.related = this.related.filter(row => row.because !== item.name);
          this.suggestions();
        }
      } catch (error) {
        console.debug('[Artist setup] choice not confirmed', error);
        if (this.destroyed) return;
        this.error = 'Choice not confirmed. Your selected artists are kept here.';
        // A catalog failure after saving must not erase the durable artist draft.
        try {
          const response = await fetch('/home/onboard/artists/state', {signal: AbortSignal.timeout(20000)});
          if (response.ok) {
            const state = await response.json();
            if (!this.destroyed && Array.isArray(state.selected)) {
              this.selected = state.selected;
              if (this.isSelected(item) === selecting) this.error = 'Choice saved.';
            }
          }
        } catch (error) { console.debug('[Artist setup] choice check unavailable', error); }
      } finally { this.saving = false; }
    },
    async suggestions() {
      const version = ++this.relatedVersion;
      this.relatedNote = '';
      this.relatedLoading = !!this.selected.length;
      if (!this.selected.length) { this.related = []; return; }
      try {
        const response = await fetch('/home/onboard/artists/suggestions');
        if (!response.ok) throw new Error();
        const body = await response.json();
        if (version !== this.relatedVersion) return;
        this.related = body.artists;
        if (!this.related.some(item => !this.isSelected(item))) {
          this.relatedNote = 'No related artists found. Search for another favorite.';
        }
      } catch (_) {
        if (version === this.relatedVersion) {
          this.relatedNote = 'Related artists are unavailable right now. Search or try again.';
        }
      } finally { if (version === this.relatedVersion) this.relatedLoading = false; }
    },
    listen() {
      this.stage = 'listen';
      this.$nextTick(() => htmx.ajax('GET', '/home/onboard/artists/listen', {target: '#artist-first-listen', swap: 'innerHTML'}));
    },
    async playback(data) {
      const playing = data?.now_playing;
      if (this.pendingPlaylist && data.connected && playing?.paused === false
          && playing.video_id && playing.playlist_ytm_id === this.pendingPlaylist && !this.checkingPlayback) {
        this.checkingPlayback = true;
        try {
          const response = await fetch('/home/onboard/artists/state');
          if (response.ok && (await response.json()).complete) {
            this.$dispatch('artist-started');
            await htmx.ajax('GET', '/home/onboard/start', {target: '#onboarding-start', swap: 'outerHTML'});
            try { sessionStorage.removeItem('tc-artist-first-playlist'); } catch (_) {}
          }
        } catch (_) {} finally { this.checkingPlayback = false; }
      }
    },
    init() {
      this.$watch('stage', () => this.$nextTick(() => this.$dispatch('artist-stage')));
      if (this.selected.length) this.suggestions();
      try {
        const pending = JSON.parse(sessionStorage.getItem('tc-artist-first-playlist') || 'null');
        if (pending && /^[A-Za-z0-9_-]{3,150}$/.test(pending.id) && pending.expires > Date.now()) {
          this.pendingPlaylist = pending.id; this.stage = 'listen'; this.resuming = true;
        }
      } catch (_) {}
      this.$nextTick(() => this.$dispatch('artist-stage'));
      this.beforeSwap = event => {
        if (!event.detail.requestConfig?.elt?.hasAttribute('data-artist-first-play') || event.detail.isError) return;
        const page = new DOMParser().parseFromString(event.detail.serverResponse, 'text/html');
        this.pendingPlaylist = page.querySelector('[data-generated-playlist]')?.dataset.generatedPlaylist || null;
        if (this.pendingPlaylist) {
          try { sessionStorage.setItem('tc-artist-first-playlist', JSON.stringify({id: this.pendingPlaylist, expires: Date.now() + 600000})); } catch (_) {}
        }
        this.playback(window.playbackStatus?.snapshot);
      };
      document.addEventListener('htmx:beforeSwap', this.beforeSwap);
    },
    destroy() {
      this.destroyed = true;
      this.relatedVersion++;
      document.removeEventListener('htmx:beforeSwap', this.beforeSwap);
    },
  };
}
