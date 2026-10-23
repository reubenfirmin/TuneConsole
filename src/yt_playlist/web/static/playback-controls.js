// Document-wide keyboard and browser media controls share the existing extension commands.
(function () {
  if (window.playbackControls) return;
  let nextPending = false;

  function command(action) {
    const status = window.playbackStatus?.snapshot;
    const playing = status?.connected && status.now_playing;
    if (!playing?.video_id) return false;
    if ((action === 'play' && playing.paused === false) ||
        (action === 'pause' && playing.paused === true)) return true;
    const next = action === 'next';
    if (next && nextPending) return true;
    const options = {method: 'POST'};
    if (next) {
      nextPending = true;
      options.headers = {'Content-Type': 'application/json'};
      options.body = JSON.stringify({videoId: playing.video_id});
    }
    fetch(next ? '/now-playing/next' : '/now-playing/toggle', options)
      .catch(() => {})
      .finally(() => {
        if (next) nextPending = false;
        window.playbackStatus.refresh();
      });
    return true;
  }

  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.repeat || event.isComposing ||
        event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const media = {MediaPlayPause: 'toggle', MediaPlay: 'play', MediaPause: 'pause', MediaTrackNext: 'next'};
    let action = media[event.key];
    if (!action && (event.code === 'Space' || event.key === ' ')) {
      // Native activation of controls and editing always take precedence over the page shortcut.
      if (event.target?.closest?.('input, textarea, select, button, a, summary, audio, video, '
          + '[contenteditable]:not([contenteditable="false"]), '
          + '[role="button"], [role="textbox"], [role="combobox"], [role="slider"], '
          + '[role="checkbox"], [role="switch"], [role="tab"], [role="menuitem"]')) return;
      action = 'toggle';
    }
    if (action && command(action)) event.preventDefault();
  });

  const session = window.navigator?.mediaSession;
  function updateMedia() {
    if (!session) return;
    const status = window.playbackStatus?.snapshot;
    const playing = status?.connected && status.now_playing;
    const active = !!playing?.video_id;
    session.playbackState = active && typeof playing.paused === 'boolean' ?
      (playing.paused ? 'paused' : 'playing') : 'none';
    if (window.MediaMetadata) session.metadata = active ?
      new window.MediaMetadata({title: playing.title || '', artist: playing.artist || ''}) : null;
  }
  if (session) {
    for (const [name, action] of [['play','play'], ['pause','pause'], ['nexttrack','next']]) {
      try { session.setActionHandler(name, () => command(action)); }
      catch (_) { /* Browsers can support only a subset of media actions. */ }
    }
    window.addEventListener('bridge-status', updateMedia);
    updateMedia();
  }
  window.playbackControls = {command};
})();
// Route every YouTube Music play/open link through the extension so it plays in the EXISTING YouTube
// Music tab (in the background, you stay on TuneConsole). A bridge failure must not create another
// player that cannot stop the existing music. Ctrl/meta/middle-click still open a new tab.
function tcPlay(url) {
  const requestId = window.tcPlaybackRequestSequence = (window.tcPlaybackRequestSequence || 0) + 1;
  window.dispatchEvent(new CustomEvent('playback-requested', {detail: {url, requestId}}));
  const failed = () => {
    window.dispatchEvent(new CustomEvent('playback-settled', {detail: {requestId, ok: false}}));
    document.getElementById('playback-error')?.remove();
    const toast = document.createElement('div');
    toast.id = 'playback-error';
    toast.className = 'toast toast-err';
    const message = document.createElement('span');
    message.className = 'toast-body';
    message.textContent = 'Couldn’t start playback. Check that TuneConsole and its browser extension are connected, then try again.';
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'toast-x';
    dismiss.setAttribute('aria-label', 'Dismiss');
    dismiss.textContent = '×';
    dismiss.onclick = () => toast.remove();
    toast.append(message, dismiss);
    document.getElementById('toasts')?.prepend(toast);
    return false;
  };
  return fetch('/play', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
  }).then(r => r.json()).then(d => {
    if (!d || !d.ok) return failed();
    window.dispatchEvent(new CustomEvent('playback-settled', {detail: {requestId, ok: true}}));
    document.getElementById('playback-error')?.remove();
    return true;
  }).catch(failed);
}
window.tcPlay = tcPlay;
// Capture phase (the `true` below) so we run BEFORE the target's own handlers: some play links live
// inside modals and carry @click.stop, which would hide the click from a normal bubble listener.
if (!window.__ytLinkBound) {
window.__ytLinkBound = true;
document.addEventListener('click', function (e) {
  if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  var a = e.target.closest && e.target.closest('a[href*="music.youtube.com/"]');
  if (!a) return;
  // Informational opens (a song title, "open it on YouTube Music"): you want to LOOK at it, not start
  // it. Leave the link alone so the browser honours target="_blank" and whatever is playing in the
  // YouTube Music tab keeps playing. Play controls carry no marker and still route to that tab.
  if (a.hasAttribute('data-yt-info')) return;
  var u;
  try { u = new URL(a.href || a.getAttribute('href'), location.href); } catch (err) { return; }
  if (u.hostname !== 'music.youtube.com') return;
  // Any content path (watch / playlist / browse / channel / ...) routes to the existing tab. Only the
  // bare origin (the "sign in" link) is left to open normally so you can actually see the sign-in page.
  if (u.pathname === '/' || u.pathname === '') return;
  e.preventDefault();
  tcPlay(u.href);
}, true);
}

