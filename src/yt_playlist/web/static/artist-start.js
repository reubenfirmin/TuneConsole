function artistStart(initial) {
  return {
    selected: initial.selected, minimum: initial.minimum, query: '', results: [], related: [],
    stage: 'artists', saving: false, searching: false, error: '', searchNote: '',
    relatedNote: '', relatedLoading: false,
    expanded: false, spotifySteps: false, searchVersion: 0, pendingPlaylist: null, relatedVersion: 0, resuming: false,
    get artistChoices() {
      const rows = [...this.selected, ...(this.query.trim() ? this.results : this.related)];
      const seen = new Set();
      return rows.filter(row => { if (seen.has(row.browse_id)) return false; seen.add(row.browse_id); return true; });
    },
    get artistLimit() { return Math.max(6, this.selected.length + 3); },
    get hasMoreArtists() { return this.artistChoices.length > this.artistLimit; },
    get visibleArtists() { return this.artistChoices.slice(0, this.expanded ? this.artistChoices.length : this.artistLimit); },
    isSelected(item) { return this.selected.some(row => row.browse_id === item.browse_id); },
    clearSearch() {
      this.query = ''; this.results = []; this.searchNote = ''; this.searching = false;
      this.searchVersion++; this.searchController?.abort();
      document.getElementById('artist-start-search').focus();
    },
    async search() {
      const query = this.query.trim(), version = ++this.searchVersion;
      this.searchController?.abort(); this.searching = false; this.searchNote = '';
      if (query.length < 2) { this.results = []; return; }
      this.searchController = new AbortController(); this.searching = true;
      try {
        const response = await fetch('/home/onboard/artists/search?q=' + encodeURIComponent(query), {signal: this.searchController.signal});
        const body = await response.json();
        if (version !== this.searchVersion) return;
        if (!response.ok) throw new Error(body.detail || 'Search is unavailable. Try again.');
        this.results = body.artists;
        this.searchNote = this.results.length ? '' : 'No artists found. Try another name.';
      } catch (error) {
        if (version === this.searchVersion && error.name !== 'AbortError') this.searchNote = error.message;
      } finally { if (version === this.searchVersion) this.searching = false; }
    },
    async toggle(item) {
      if (this.saving) return;
      const selecting = !this.isSelected(item);
      this.saving = true; this.error = '';
      try {
        const response = await fetch('/home/onboard/artists/select', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({browse_id: item.browse_id, selected: selecting})});
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || 'Could not save this choice. Try again.');
        this.selected = body.selected;
        if (selecting) {
          this.related = body.related;
          this.suggestions();
          this.clearSearch();
        } else {
          this.related = this.related.filter(row => row.because !== item.name);
          this.suggestions();
        }
      } catch (error) {
        this.error = error.message;
        // A catalog failure after saving must not erase the durable artist draft.
        try {
          const response = await fetch('/home/onboard/artists/state');
          if (response.ok) this.selected = (await response.json()).selected;
        } catch (_) {}
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
      this.searchVersion++; this.relatedVersion++;
      this.searchController?.abort();
      document.removeEventListener('htmx:beforeSwap', this.beforeSwap);
    },
  };
}
