// Guide the first mix or album into the shared player.
(() => {
  if (window.__onboardingPlaybackBound) return;
  window.__onboardingPlaybackBound = true;
  const PENDING = 'tc-onboarding-playback', PLAYED = 'tc-onboarding-played';
  window.__onboardingTourDismissed = document.body.dataset.playerTourDismissed === 'true';
  let pending = null, tour = null;
  const steps = [
    {title: 'Your music is playing', text: 'This player stays with you as you explore TuneConsole.', target: '.global-now-playing'},
    {title: 'Keep listening', text: 'Pause and resume here, or skip to the next song.', target: '.hs-rate-group'},
    {title: 'Make it yours', text: 'Like songs you love. Dislike skips a song and helps TuneConsole learn your preferences.', target: '.hs-rate-group'},
  ];
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

  function rememberPlaylist(playlist) {
    if (window.__onboardingTourDismissed) return;
    if (pending?.started) return;
    pending = {playlist, expires: Date.now() + 10 * 60 * 1000};
    save();
    apply(window.playbackStatus?.snapshot);
  }

  function finish() {
    pending = null;
    save();
    tour?.remove();
    tour = null;
    document.querySelector('.global-now-playing')?.classList.remove('is-onboarding-cue');
  }

  function dismiss() {
    window.__onboardingTourDismissed = true;
    finish();
    fetch('/onboard/player-tour/dismiss', {method: 'POST', signal: AbortSignal.timeout(3000)})
      .catch(error => console.debug('Player tour acknowledgement deferred', error));
  }

  function position() {
    if (!tour || !pending) return;
    const album = document.querySelector('dialog.album-listening[open]');
    const parent = album || document.body;
    if (tour.parentElement !== parent) parent.append(tour);
    const anchor = album
      ? album.querySelector(pending.step ? '.album-listening-controls' : '.album-listening-footer')
      : document.querySelector(steps[pending.step || 0].target);
    if (!anchor?.getClientRects().length) { tour.hidden = true; return; }
    tour.hidden = false;
    const rect = anchor.getBoundingClientRect(), box = tour.getBoundingClientRect();
    const left = album ? 24
      : rect.right + box.width + 24 <= innerWidth ? rect.right + 16 : rect.left - box.width - 16;
    tour.style.left = Math.max(16, Math.min(innerWidth - box.width - 16, left)) + 'px';
    const top = album ? rect.top - box.height - 16 : rect.bottom - box.height;
    tour.style.top = Math.max(16, Math.min(innerHeight - box.height - 16, top)) + 'px';
  }

  function showTour() {
    if (!tour) {
      tour = document.createElement('aside');
      tour.className = 'onboarding-player-tour';
      tour.setAttribute('role', 'region');
      tour.setAttribute('aria-labelledby', 'player-tour-title');
      tour.innerHTML = '<span class="player-tour-count"></span><h2 id="player-tour-title"></h2><p></p>'
        + '<div><button type="button" class="btn-ghost" data-tour-dismiss>Dismiss tour</button>'
        + '<button type="button" class="btn btn-primary" data-tour-next>Next</button></div>';
      tour.querySelector('[data-tour-dismiss]').addEventListener('click', dismiss);
      tour.querySelector('[data-tour-next]').addEventListener('click', () => {
        if ((pending.step || 0) === steps.length - 1) { dismiss(); return; }
        pending.step = (pending.step || 0) + 1;
        save();
        showTour();
      });
      document.body.append(tour);
    }
    const index = pending.step || 0, step = steps[index];
    tour.querySelector('.player-tour-count').textContent = 'Your player · ' + (index + 1) + ' of ' + steps.length;
    tour.querySelector('h2').textContent = step.title;
    tour.querySelector('p').textContent = step.text;
    tour.querySelector('[data-tour-next]').textContent = index === steps.length - 1 ? 'Got it' : 'Next';
    requestAnimationFrame(position);
  }

  function apply(data) {
    // A new document mounts before its first status response. Wait for evidence
    // instead of treating that empty cache as interrupted playback.
    if (!pending || !data) return;
    const now = Date.now(), playing = data?.now_playing;
    if (window.__onboardingTourDismissed || (!pending.playlist && !pending.album)
        || (!pending.started && (!Number.isFinite(pending.expires) || now >= pending.expires))) { finish(); return; }
    if (pending.started) {
      document.querySelector('.global-now-playing')?.classList.add('is-onboarding-cue');
      showTour();
      return;
    }
    let albumMatches = false;
    if (pending.album && playing?.source?.kind === 'album' && playing.source.url) {
      try {
        const source = new URL(playing.source.url, location.href);
        albumMatches = source.origin === location.origin && source.searchParams.get('browse') === pending.album;
      } catch (_) {}
    }
    const matches = data?.connected && playing?.paused === false && playing.video_id
      && (playing.title || playing.artist)
      && (pending.album ? albumMatches : playing.playlist_ytm_id === pending.playlist
          && (!pending.radio || playing.source?.kind === 'radio'));
    if (!matches) {
      return;
    }
    const card = document.querySelector('.global-now-playing');
    if (!card) return;
    if (!pending.started) {
      pending.started = true;
      save();
      document.documentElement.dataset.onboardingPlayed = 'true';
      try { sessionStorage.setItem(PLAYED, '1'); } catch (_) {}
      window.dispatchEvent(new CustomEvent('onboarding-playback-started'));
    }
    card.classList.add('is-onboarding-cue');
    showTour();
  }

  document.addEventListener('onboarding-album-play', event => {
    if (window.__onboardingTourDismissed) return;
    if (pending?.started) return;
    const album = event.target.dataset.onboardingAlbum;
    if (!album) return;
    pending = {album, expires: Date.now() + 10 * 60 * 1000};
    save();
  });
  document.addEventListener('radio-session-state', event => {
    // An accepted session is a handoff target, not proof of audible playback.
    // Only the starter's exact playlist, confirmed by the bridge, completes it.
    if (window.__onboardingTourDismissed || !event.target.closest?.('.onboarding-radio') || !event.detail.active || !event.detail.playlist
        || document.documentElement.dataset.onboardingPlayed === 'true') return;
    if (pending?.radio && pending.playlist === event.detail.playlist) return;
    pending = {radio: true, playlist: event.detail.playlist, expires: Date.now() + 10 * 60 * 1000};
    save();
    apply(window.playbackStatus?.snapshot);
  });
  document.addEventListener('htmx:afterRequest', event => {
    if (event.detail.elt?.hasAttribute('data-onboarding-album') && event.detail.failed) finish();
  });
  document.addEventListener('generated-playlist-saved', event => {
    if (event.target.hasAttribute?.('data-onboarding-play')) rememberPlaylist(event.detail.playlist);
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (!event.detail.requestConfig?.elt?.hasAttribute('data-onboarding-play')) return;
    const response = new DOMParser().parseFromString(event.detail.serverResponse, 'text/html');
    const playlist = response.querySelector('[data-generated-playlist]')?.dataset.generatedPlaylist;
    if (!event.detail.shouldSwap || event.detail.isError || !playlist) { finish(); return; }
    rememberPlaylist(playlist);
  });
  window.addEventListener('bridge-status', event => apply(event.detail));
  const refresh = () => apply(window.playbackStatus?.snapshot);
  window.addEventListener('app-page-changed', refresh);
  window.addEventListener('album-listening-view', position);
  window.addEventListener('resize', position);
  window.addEventListener('scroll', position, true);
  document.addEventListener('DOMContentLoaded', refresh, {once: true});
})();
