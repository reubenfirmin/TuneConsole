// Guide the first mix or album into the shared player.
(() => {
  if (window.__onboardingPlaybackBound) return;
  window.__onboardingPlaybackBound = true;
  const PENDING = 'tc-onboarding-playback', PLAYED = 'tc-onboarding-played';
  let pending = null, timer = null;
  try {
    pending = JSON.parse(sessionStorage.getItem(PENDING));
    if (sessionStorage.getItem(PLAYED)) document.documentElement.dataset.onboardingPlayed = 'true';
  } catch (_) {}

  function save() {
    try {
      if (pending) sessionStorage.setItem(PENDING, JSON.stringify(pending));
      else sessionStorage.removeItem(PENDING);
    } catch (_) {}
  }

  function finish() {
    clearTimeout(timer);
    timer = null;
    pending = null;
    save();
    document.querySelector('.global-now-playing')?.classList.remove('is-onboarding-cue');
  }

  function apply(data) {
    // A new document mounts before its first status response. Wait for evidence
    // instead of treating that empty cache as interrupted playback.
    if (!pending || !data) return;
    const now = Date.now(), playing = data?.now_playing;
    if ((!pending.playlist && !pending.album) || !Number.isFinite(pending.expires) || now >= pending.expires
        || (pending.until && now >= pending.until)) { finish(); return; }
    let albumMatches = false;
    if (pending.album && playing?.source?.kind === 'album' && playing.source.url) {
      try {
        const source = new URL(playing.source.url, location.href);
        albumMatches = source.origin === location.origin && source.searchParams.get('browse') === pending.album;
      } catch (_) {}
    }
    const matches = data?.connected && playing?.paused === false && playing.video_id
      && (playing.title || playing.artist)
      && (pending.album ? albumMatches : playing.playlist_ytm_id === pending.playlist);
    if (!matches) {
      if (pending.until) finish();
      return;
    }
    const card = document.querySelector('.global-now-playing');
    if (!card) return;
    if (!pending.until) {
      pending.until = now + 8000;
      save();
      document.documentElement.dataset.onboardingPlayed = 'true';
      try { sessionStorage.setItem(PLAYED, '1'); } catch (_) {}
    }
    card.classList.add('is-onboarding-cue');
    // Repeated snapshots never restart the eight-second handoff. A full page load
    // resumes the remaining time, rather than losing it during the redirect.
    clearTimeout(timer);
    timer = setTimeout(finish, pending.until - now);
  }

  document.addEventListener('htmx:beforeRequest', event => {
    const album = event.detail.elt?.dataset.onboardingAlbum;
    if (!album) return;
    pending = {album, expires: Date.now() + 10 * 60 * 1000};
    save();
  });
  document.addEventListener('htmx:afterRequest', event => {
    if (event.detail.elt?.hasAttribute('data-onboarding-album') && event.detail.failed) finish();
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (!event.detail.requestConfig?.elt?.hasAttribute('data-onboarding-play')) return;
    const response = new DOMParser().parseFromString(event.detail.serverResponse, 'text/html');
    const playlist = response.querySelector('[data-generated-playlist]')?.dataset.generatedPlaylist;
    if (!event.detail.shouldSwap || event.detail.isError || !playlist) { finish(); return; }
    pending = {playlist, expires: Date.now() + 10 * 60 * 1000};
    save();
    apply(window.playbackStatus?.snapshot);
  });
  window.addEventListener('bridge-status', event => apply(event.detail));
  const refresh = () => apply(window.playbackStatus?.snapshot);
  window.addEventListener('app-page-changed', refresh);
  document.addEventListener('DOMContentLoaded', refresh, {once: true});
})();
