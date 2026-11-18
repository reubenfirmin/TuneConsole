// Album presentation consumes the shared player; it never issues playback or polls.
function albumListening() {
  const storageKey = 'tc-album-listening-v1';
  let dialog = null;
  return {
    album: null,
    pending: null,
    candidate: null,
    available: false,
    opened: false,
    artworkFailed: '',
    mount(element) {
      dialog = element;
      try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
        if (saved?.album?.browse && typeof saved.album.playlist === 'string') {
          this.album = saved.album;
          this.opened = saved.opened === true;
        }
      } catch (_) { /* Listening remains usable when browser storage is unavailable. */ }
      this.update(window.playbackStatus?.snapshot);
    },
    save() {
      try {
        if (this.album) sessionStorage.setItem(storageKey, JSON.stringify({album: this.album, opened: this.opened}));
        else sessionStorage.removeItem(storageKey);
      } catch (_) { /* Presentation state is optional. */ }
    },
    announce(available) {
      if (this.available === available) return;
      this.available = available;
      this.$dispatch('album-listening-availability', {available});
    },
    request(detail) {
      if (!detail?.browse) return;
      this.clear();
      this.pending = {browse: detail.browse, until: Date.now() + 20000};
    },
    accept(detail) {
      if (!detail?.browse || detail.browse !== this.pending?.browse || Date.now() > this.pending.until) return;
      this.candidate = {browse: detail.browse, playlist: detail.playlist || '', title: detail.title || '',
        artist: detail.artist || '', thumbnail: detail.thumbnail || ''};
      window.playbackStatus?.refresh();
      this.update(window.playbackStatus?.snapshot);
    },
    matches(album, song) {
      if (!song?.video_id || ['radio', 'tv'].includes(song.source?.kind)) return false;
      if (album.playlist) return song.playlist_ytm_id === album.playlist;
      if (song.album_browse_id === album.browse) return true;
      if (song.source?.kind !== 'album' || !song.source.url) return false;
      try {
        const source = new URL(song.source.url, location.href);
        return source.origin === location.origin && source.searchParams.get('browse') === album.browse;
      } catch (_) { return false; }
    },
    update(status) {
      if (window.tvPlaybackControls?.active()) { this.clear(); return; }
      const song = status?.connected && (status.now_playing || status.resumable_player);
      // A brief missing observation must not discard an album or reopen a dismissed view.
      if (!song?.video_id) { this.announce(false); return; }
      if (this.pending && Date.now() > this.pending.until) this.pending = this.candidate = null;
      if (this.candidate && this.matches(this.candidate, song) && song.paused === false) {
        this.album = this.candidate;
        this.pending = this.candidate = null;
        this.artworkFailed = '';
        this.announce(true);
        this.show();
        return;
      }
      if (!this.album) return;
      if (!this.matches(this.album, song)) { this.clear(); return; }
      this.announce(true);
      if (this.opened && !dialog.open) this.show();
    },
    show() {
      if (!this.available || !dialog || dialog.open) return;
      this.opened = true;
      dialog.showModal();
      this.$nextTick(() => window.dispatchEvent(new CustomEvent('album-listening-view')));
      this.save();
    },
    dismiss() {
      this.opened = false;
      dialog?.close();
      window.dispatchEvent(new CustomEvent('album-listening-view'));
      this.save();
    },
    closed() {
      // A queued close event may belong to the previous album's presentation.
      if (dialog.open) return;
      this.opened = false;
      window.dispatchEvent(new CustomEvent('album-listening-view'));
      this.save();
    },
    clear() {
      this.dismiss();
      this.album = this.pending = this.candidate = null;
      this.announce(false);
      this.save();
    },
    artwork() {
      const thumbnail = this.album?.thumbnail || '';
      return thumbnail !== this.artworkFailed && !/cover_(?:album|track)_default/.test(thumbnail) ? thumbnail : '';
    },
    tracksUrl() { return '/album?browse=' + encodeURIComponent(this.album?.browse || '') + '#album-tracks'; },
  };
}
