// Album controls consume the document's player and issue its existing commands.
function announceAlbumListening(element, name) {
  if (!element.dataset.albumListening) return;
  element.dispatchEvent(new CustomEvent(name, {bubbles: true,
    detail: JSON.parse(element.dataset.albumListening)}));
}

function playAlbumLink(element) {
  const album = JSON.parse(element.dataset.albumListening);
  announceAlbumListening(element, 'album-listening-requested');
  window.tvPlaybackControls?.release();
  if (album.browse.startsWith('FEmusic_library_privately_owned_release_detail')) {
    window.tcPlay(element.href).then(ok => {
      if (ok) announceAlbumListening(element, 'album-play-started');
    });
    return;
  }
  htmx.ajax('POST', '/album/play', {source: element, swap: 'none',
    values: {browse_id: album.browse}}).catch(() => {});
}

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
      const announce = name => announceAlbumListening(button, name);
      if (!this.playing) announce('album-listening-requested');
      if (!this.playing && button.dataset.onboardingAlbum === browse) {
        button.dispatchEvent(new CustomEvent('onboarding-album-play', {bubbles: true}));
      }
      if (this.active && window.playbackControls.command('toggle')) {
        if (!this.playing) announce('album-play-started');
        return;
      }
      // Privately owned uploads have no audioPlaylistId. Preserve their existing
      // browser handoff instead of submitting an album queue that cannot exist.
      if (fallback) {
        Promise.resolve(window.tcPlay(fallback)).then(ok => { if (ok) announce('album-play-started'); });
        return;
      }
      window.tvPlaybackControls?.release();
      htmx.trigger(button, 'album-start');
    },
  };
}
