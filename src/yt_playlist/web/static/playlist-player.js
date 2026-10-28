// Playlist headers follow the shared status; a song match alone is not a queue match.
function playlistPlayback(playlist) {
  return {
    active: false,
    playing: false,
    busy: false,
    init() { this.apply(window.playbackStatus?.snapshot); },
    apply(status) {
      const song = status?.connected && (status.now_playing || status.resumable_player);
      this.active = !!(playlist && song?.video_id && song.playlist_ytm_id === playlist
        && song.source?.kind !== 'radio' && song.source?.kind !== 'tv');
      this.playing = this.active && song.paused === false;
    },
    async play() {
      if (this.busy) return;
      const status = window.playbackStatus?.snapshot;
      this.apply(status);
      this.busy = true;
      let deadline;
      let errorMessage = 'Playback was not confirmed. Check YouTube Music before trying again.';
      try {
        if (!this.active) {
          await window.tcPlay('https://music.youtube.com/playlist?list=' + encodeURIComponent(playlist));
          return;
        }
        const song = status.now_playing || status.resumable_player;
        const action = this.playing ? 'pause' : 'play';
        const params = new URLSearchParams({action, videoId: song.video_id, confirm: 'true'});
        if (!status.now_playing) params.set('resume', 'true');
        const controller = new AbortController();
        deadline = setTimeout(() => controller.abort(), 12000);
        const response = await fetch('/now-playing/toggle?' + params, {
          method: 'POST', signal: controller.signal,
        });
        if (!response.ok) throw new Error(errorMessage);
        const result = await response.json();
        if (!result?.ok || result.paused !== (action === 'pause')) {
          errorMessage = result?.error || errorMessage;
          throw new Error(errorMessage);
        }
        document.getElementById('playback-error')?.remove();
      } catch (_) {
        window.tcPlaybackError(errorMessage);
      } finally {
        clearTimeout(deadline);
        this.busy = false;
        window.playbackStatus?.refresh();
      }
    },
  };
}
