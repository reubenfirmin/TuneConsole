function openMusicSearch(options = {}) {
  window.dispatchEvent(new CustomEvent('music-search-open', {detail: {...options, source: options.source || document.activeElement}}));
}

function musicSearch() {
  return {
    query: '', onboarding: false, sourceTab: 'library', songs: [], artists: [], albums: [], playlists: [], selected: [],
    loading: false, sectionsLoading: false, libraryLoading: false, catalogDone: false,
    sectionsDone: false, libraryDone: false, libraryCount: 0, version: 0, libraryVersion: 0, statuses: {}, intents: {}, artistBusy: '', artistNote: '',
    get catalogCount() { return this.songs.length + this.artists.length + this.albums.length + this.playlists.length; },
    get catalogReady() { return this.catalogCount > 0 || (this.catalogDone && this.sectionsDone); },
    get catalogSections() {
      const rows = {artist: this.artists, song: this.songs, album: this.albums, playlist: this.playlists};
      return JSON.parse(this.$root.dataset.sectionOrder).map(([kind, title]) => ({kind, title, rows: rows[kind]}));
    },
    async openSearch(options) {
      this.onboarding = !!options.onboarding;
      this.source = options.source;
      if (this.onboarding) this.sourceTab = 'catalog';
      if (!this.$root.open) this.$root.showModal();
      if (options.query !== undefined) this.query = options.query;
      this.$nextTick(() => this.$refs.query.focus());
      if (this.onboarding) {
        try {
          const state = await this.json('/home/onboard/artists/state');
          this.selected = state.selected || [];
        } catch (error) { console.debug('[Music search] draft read unavailable', error); }
      }
      this.search();
    },
    close() {
      if (!this.$root.open) return;
      this.$root.close();
      // Finish this search synchronously. The native close event is queued and
      // may arrive after the user focuses the player or opens another search.
      this.version++; this.controller?.abort();
      this.loading = this.sectionsLoading = this.libraryLoading = false;
      if (this.source?.isConnected) this.source.focus({preventScroll: true});
    },
    async json(url, options = {}) {
      const response = await fetch(url, {...options,
        signal: options.signal || AbortSignal.timeout(25000)});
      if (!response.ok) throw new Error('Music action response ' + response.status);
      return response.json();
    },
    search() {
      const version = ++this.version, query = this.query.trim();
      this.controller?.abort(); this.controller = new AbortController();
      this.songs = []; this.artists = []; this.albums = []; this.playlists = []; this.artistNote = '';
      this.$refs.library.replaceChildren();
      this.catalogDone = this.sectionsDone = this.libraryDone = false;
      this.loading = this.sectionsLoading = this.libraryLoading = false;
      if (query.length < 2) return;
      const signal = AbortSignal.any([this.controller.signal, AbortSignal.timeout(20000)]);
      this.loading = true;
      // Independent reads keep library navigation useful during catalog recovery.
      this.refreshLibrary();
      this.json('/search/catalog?q=' + encodeURIComponent(query), {signal})
        .then(body => { if (version === this.version) { this.songs = body.songs; this.catalogDone = true; } })
        .catch(error => console.debug('[Music search] catalog read unavailable', error))
        .finally(() => { if (version === this.version) this.loading = false; });
      this.sectionsLoading = true;
      this.json('/search/catalog/sections?q=' + encodeURIComponent(query), {signal})
        .then(body => { if (version === this.version) {
          this.artists = body.artists; this.albums = body.albums; this.playlists = body.playlists; this.sectionsDone = true;
        } })
        .catch(error => console.debug('[Music search] catalog sections unavailable', error))
        .finally(() => { if (version === this.version) this.sectionsLoading = false; });
    },
    async refreshLibrary() {
      const query = this.query.trim(), version = this.version, request = ++this.libraryVersion;
      if (query.length < 2 || !this.$root.open) return;
      this.libraryLoading = true;
      try {
        const response = await fetch('/search/omni?q=' + encodeURIComponent(query), {
          signal: AbortSignal.any([this.controller.signal, AbortSignal.timeout(20000)])});
        if (!response.ok) throw new Error('Library search response ' + response.status);
        const html = await response.text();
        if (version === this.version && request === this.libraryVersion) {
          this.$refs.library.innerHTML = html;
          this.libraryCount = this.$refs.library.querySelectorAll('.omni-row').length;
          this.libraryDone = true;
        }
      } catch (error) { console.debug('[Music search] library read unavailable', error); }
      finally { if (version === this.version && request === this.libraryVersion) this.libraryLoading = false; }
    },
    key(item) { return item.video_id || item.kind + ':' + item.browse_id; },
    state(item) {
      return this.statuses[this.key(item)] || item.action_state || ((item.library_saved || item.owned) ? 'saved' : item.can_add ? 'new' : 'unavailable');
    },
    label(item) {
      return {saved: item.kind || !item.can_remove ? 'In library' : 'Remove from library', saving: this.intents[this.key(item)] === false ? 'Removing…' : 'Adding…',
        checking: 'Checking…', pending: 'Check library change', unavailable: 'Unavailable',
        new: 'Add to library', removed: 'Add to library',
        retry: this.intents[item.video_id] === false ? 'Retry removal' : 'Add to library'}[this.state(item)];
    },
    async save(item) {
      const previous = this.state(item), query = this.query.trim(), onboarding = this.onboarding;
      const key = this.key(item);
      if (['saving', 'checking', 'unavailable'].includes(previous) || (previous === 'saved' && !item.can_remove)) return;
      if (item.kind) {
        this.statuses[key] = previous === 'pending' ? 'checking' : 'saving';
        try {
          const result = await this.json('/search/entities/save', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({kind: item.kind, browse_id: item.browse_id, check: previous === 'pending'})});
          this.statuses[key] = result.state;
          if (result.state === 'saved') this.refreshLibrary();
        } catch (error) {
          console.debug('[Music search] catalog save awaits confirmation', error);
          this.statuses[key] = 'pending';
        }
        return;
      }
      if (!['pending', 'retry'].includes(previous)) this.intents[item.video_id] = previous !== 'saved';
      this.statuses[item.video_id] = previous === 'pending' ? 'checking' : 'saving';
      try {
        const result = await this.json('/search/songs/save', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({video_id: item.video_id, query, onboarding, saved: this.intents[item.video_id] !== false, check: previous === 'pending'})});
        this.statuses[item.video_id] = result.state;
        if (['saved', 'removed'].includes(result.state)) {
          const {state, ...membership} = result;
          Object.assign(item, membership);
          this.refreshLibrary();
          window.dispatchEvent(new CustomEvent('music-song-saved', {detail: {...item, state: result.state}}));
        }
      } catch (error) {
        console.debug('[Music search] song save awaits confirmation', error);
        this.statuses[item.video_id] = 'pending';
      }
    },
    isSelected(item) { return this.selected.some(row => row.browse_id === item.browse_id); },
    async chooseArtist(item) {
      if (this.artistBusy) return;
      this.artistBusy = item.browse_id; this.artistNote = '';
      try {
        const body = await this.json('/home/onboard/artists/select', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({browse_id: item.browse_id, selected: !this.isSelected(item)})});
        this.selected = body.selected;
        window.dispatchEvent(new CustomEvent('music-artists-updated', {detail: body}));
      } catch (error) {
        console.debug('[Music search] artist choice awaits confirmation', error);
        try {
          const state = await this.json('/home/onboard/artists/state');
          this.selected = state.selected;
          window.dispatchEvent(new CustomEvent('music-artists-updated', {detail: state}));
        } catch (_) {}
        this.artistNote = 'Your choices are kept. Check your picks before choosing again.';
      } finally { this.artistBusy = ''; }
    },
    move(direction) {
      const rows = [...this.$root.querySelectorAll('[data-music-results] a, [data-music-results] button:not(:disabled)')]
        .filter(row => row.checkVisibility());
      if (!rows.length) return;
      let index = rows.indexOf(document.activeElement);
      index = (index + direction + rows.length) % rows.length;
      rows[index].focus();
    },
    init() {
      this.onLeaving = () => { if (this.$root.open) this.close(); };
      this.onPlaylistUpdated = event => {
        const item = this.songs.find(row => row.video_id === event.detail.video_id);
        if (item) {
          item.owned = true; item.can_remove = false;
          if (['saved', 'removed', 'retry'].includes(this.statuses[item.video_id])) delete this.statuses[item.video_id];
        }
        this.refreshLibrary();
      };
      this.onShortcut = event => {
        if (event.defaultPrevented || event.isComposing || event.altKey || event.shiftKey ||
            !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return;
        event.preventDefault();
        if (this.$root.open) this.$refs.query.focus();
        else openMusicSearch();
      };
      document.addEventListener('app-page-leaving', this.onLeaving);
      document.addEventListener('keydown', this.onShortcut);
      window.addEventListener('song-playlist-updated', this.onPlaylistUpdated);
    },
    destroy() {
      this.controller?.abort(); document.removeEventListener('app-page-leaving', this.onLeaving);
      document.removeEventListener('keydown', this.onShortcut);
      window.removeEventListener('song-playlist-updated', this.onPlaylistUpdated);
    },
  };
}
