// Album controls consume the document's player and issue its existing commands.
function announceAlbumListening(element, name) {
  if (!element.dataset.albumListening) return;
  element.dispatchEvent(new CustomEvent(name, {bubbles: true,
    detail: JSON.parse(element.dataset.albumListening)}));
}

function playPrivateAlbum(element, url, showArtwork = true) {
  // tcPlay announces the new selection synchronously. Register its own
  // artwork request after that event has cleared the previous one.
  const result = window.tcPlay(url);
  if (!showArtwork) return;
  const requestId = window.tcPlaybackRequestSequence;
  announceAlbumListening(element, 'album-listening-requested');
  result.then(ok => {
    if (ok && requestId === window.tcPlaybackRequestSequence) {
      announceAlbumListening(element, 'album-play-started');
    }
  });
}

function playAlbumLink(element) {
  const album = JSON.parse(element.dataset.albumListening);
  window.tvPlaybackControls?.release();
  if (album.browse.startsWith('FEmusic_library_privately_owned_release_detail')) {
    playPrivateAlbum(element, element.href);
    return;
  }
  announceAlbumListening(element, 'album-listening-requested');
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
      if (!this.playing && button.dataset.onboardingAlbum === browse) {
        button.dispatchEvent(new CustomEvent('onboarding-album-play', {bubbles: true}));
      }
      if (this.active && window.playbackControls.command('toggle')) {
        if (!this.playing) {
          announce('album-listening-requested');
          announce('album-play-started');
        }
        return;
      }
      // Privately owned uploads have no audioPlaylistId. Preserve their existing
      // browser handoff instead of submitting an album queue that cannot exist.
      if (fallback) {
        playPrivateAlbum(button, fallback, !this.playing);
        return;
      }
      if (!this.playing) announce('album-listening-requested');
      window.tvPlaybackControls?.release();
      htmx.trigger(button, 'album-start');
    },
  };
}
