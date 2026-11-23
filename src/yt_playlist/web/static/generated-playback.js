// The save response confirms a playlist, not playback. Keep the handoff visible
// through the redirect and check the extension's actual playing playlist.
(() => {
  let pending, note, lastStatus;
  function load() {
    try { pending = JSON.parse(sessionStorage.getItem('tc-generated-playback') || 'null'); } catch (_) {}
  }
  function update(status) {
    if (status) lastStatus = status;
    status = lastStatus;
    if (!pending) return;
    // A fast player can confirm before the save script redirects. Let the
    // destination page consume that confirmation so its result stays visible.
    if (pending.redirecting && !document.querySelector('.pl-headmain')) return;
    const player = status?.now_playing || {};
    const playing = status?.connected === true && player.playlist_ytm_id === pending.playlist && player.paused === false;
    const expired = Date.now() - pending.at >= 45000;
    if (playing) {
      note?.remove();
      note = null;
      try { sessionStorage.removeItem('tc-generated-playback'); } catch (_) {}
      pending = null;
      return;
    }
    if (!expired && status?.connected !== false) return;
    if (note?.isConnected && note.dataset.generatedPlayback === pending.playlist) return;
    const owner = document.querySelector('.pl-headmain, [data-generated-playlist]');
    if (!owner) return;

    note?.remove();
    note = document.createElement('p');
    note.className = 'section-note';
    note.dataset.generatedPlayback = pending.playlist;
    note.setAttribute('role', 'status');
    const retry = owner.matches('.pl-headmain') ? 'Use Play to retry this saved playlist, or ' : 'You can ';
    const link = document.createElement('a');
    link.textContent = 'open it on YouTube Music';
    link.href = 'https://music.youtube.com/watch?list=' + encodeURIComponent(pending.playlist);
    link.target = '_blank';
    link.rel = 'noopener';
    note.append('Your mix is saved. Playback has not started. ' + retry, link);
    owner.insertBefore(note, owner.querySelector(':scope > .pl-actions'));
  }
  load();
  window.addEventListener('generated-playback-pending', () => { load(); update(); });
  window.addEventListener('bridge-status', event => update(event.detail));
  setInterval(() => update(), 1000);
})();
