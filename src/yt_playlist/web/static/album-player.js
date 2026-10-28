// Album controls consume the document's player and issue its existing commands.
function albumPlayback(browse, playlist = '', fallback = '') {
  return {
    active: false,
    playing: false,
    init() { this.apply(window.playbackStatus?.snapshot); },
    apply(status) {
      const song = status?.connected && (status.now_playing || status.resumable_player);
      let same = false;
      if (song?.video_id) {
        if (playlist && song.playlist_ytm_id) same = song.playlist_ytm_id === playlist;
        else if (song.source?.kind === 'album' && song.source.url) {
          const source = new URL(song.source.url, location.href);
          same = source.origin === location.origin && source.searchParams.get('browse') === browse;
        }
      }
      this.active = same;
      this.playing = same && song.paused === false;
    },
    play(button) {
      if (this.active && window.playbackControls.command('toggle')) return;
      // Privately owned uploads have no audioPlaylistId. Preserve their existing
      // browser handoff instead of submitting an album queue that cannot exist.
      if (fallback) { window.tcPlay(fallback); return; }
      window.tvPlaybackControls?.release();
      htmx.trigger(button, 'album-start');
    },
  };
}
