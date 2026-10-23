// Exposure belongs to visible discovery tiles and story headings, never a GET or hidden tab.
// A page's seen set survives fragment polls. Observe the heading rather than a whole article,
// which can be taller than the viewport on a phone.
let discoveryExposure = null;
function syncDiscoveryExposure() {
  const home = document.getElementById('home');
  if (discoveryExposure?.home !== home) {
    discoveryExposure?.observer.disconnect();
    discoveryExposure?.timers.forEach(clearTimeout);
    discoveryExposure = null;
  }
  if (!home || !window.IntersectionObserver) return;
  if (!discoveryExposure) {
    const state = {home, viewId: crypto.randomUUID(), observed: new Set(), seen: new Set(),
      visible: new Set(), timers: new Map()};
    state.observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        const tile = entry.target;
        if (!entry.isIntersecting || entry.intersectionRatio < .5) {
          state.visible.delete(tile);
          clearTimeout(state.timers.get(tile));
          state.timers.delete(tile);
          return;
        }
        state.visible.add(tile);
        if (document.hidden || state.timers.has(tile)) return;
        const story = tile.dataset.storySubject;
        const kind = story ? 'story' : tile.dataset.discoveryKind;
        const id = story || tile.dataset.discoveryId;
        const key = JSON.stringify([kind, id]);
        if (state.seen.has(key)) return;
        state.timers.set(tile, setTimeout(() => {
          state.timers.delete(tile);
          if (document.hidden || !tile.isConnected || !tile.getClientRects().length ||
              !state.visible.has(tile) || state.seen.has(key)) return;
          state.seen.add(key);
          fetch(story ? '/home/into-recently/impressions' : '/discovery/impressions', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, keepalive: true,
            body: JSON.stringify(story ? {subjects: [id]} : {kind, ids: [id], view_id: state.viewId})
          }).then(response => {
            if (!response.ok) state.seen.delete(key);
          }).catch(() => state.seen.delete(key));
        }, 750));
      });
    }, {threshold: .5});
    discoveryExposure = state;
  }
  const state = discoveryExposure;
  state.observed.forEach(tile => {
    if (tile.isConnected) return;
    state.observer.unobserve(tile);
    state.observed.delete(tile);
    state.visible.delete(tile);
    clearTimeout(state.timers.get(tile));
    state.timers.delete(tile);
  });
  home.querySelectorAll('[data-discovery-id], [data-story-subject]').forEach(tile => {
    if (state.observed.has(tile)) return;
    state.observed.add(tile);
    state.observer.observe(tile);
  });
}
document.addEventListener('DOMContentLoaded', syncDiscoveryExposure);
document.addEventListener('htmx:afterSettle', syncDiscoveryExposure);
document.addEventListener('visibilitychange', () => {
  if (!discoveryExposure) return;
  discoveryExposure.timers.forEach(clearTimeout);
  discoveryExposure.timers.clear();
  if (!document.hidden) discoveryExposure.observed.forEach(tile => {
    discoveryExposure.observer.unobserve(tile);
    discoveryExposure.observer.observe(tile);
  });
});

// Home generated mixes. Alpine owns the board's `focus` state; htmx fetches regenerated fragments;
// this layer keeps the focused shell mounted and choreographs the in-place content transition.
function genPayload(id) { return document.querySelector('#' + CSS.escape(id) + ' > .gen-card-body'); }
function genName(id) { const el = genPayload(id); return el ? el.dataset.name : ''; }
function genRecipe(id) { const el = genPayload(id); return el ? (el.dataset.recipe || '') : ''; }

function genReveal(id) {
  requestAnimationFrame(() => {
    document.querySelectorAll('#' + CSS.escape(id) + ' .gen-focus img').forEach((img) => { img.loading = 'eager'; });
    const play = document.querySelector('#' + CSS.escape(id) + ' .gen-play');
    if (play) requestAnimationFrame(() => play.focus({ preventScroll: true }));
  });
}

function genTracks(id) {
  return JSON.stringify(Array.from(document.getElementById(id).querySelectorAll('.gen-row')).map((row) => ({
    video_id: row.dataset.vid, title: row.dataset.title, artist: row.dataset.artist,
    album: row.dataset.album, thumbnail: row.dataset.thumb
  })));
}

const mixRegenerations = new WeakMap();

function finishMixRegeneration(card, failed = false) {
  const state = mixRegenerations.get(card);
  if (!state) return;
  clearTimeout(state.timer);
  state.animations.forEach(animation => animation.cancel());
  delete state.button.dataset.regenerating;
  state.button.removeAttribute('aria-busy');
  card.classList.remove('is-regenerating');
  mixRegenerations.delete(card);
  if (failed) {
    const error = document.createElement('p');
    error.className = 'section-note gen-refresh-error';
    error.setAttribute('role', 'alert');
    error.textContent = 'Couldn’t regenerate this mix. Your mix is still here. Try again.';
    (card.querySelector('.gen-focus') || card.querySelector('.gen-card-body')).prepend(error);
  }
}

function regenerateMix(button) {
  if (!button || button.dataset.regenerating === '1') return;
  const card = button.closest('.gen-card');
  if (!card || mixRegenerations.has(card)) return;
  card.querySelector('.gen-refresh-error')?.remove();
  const state = {button, animations: [], transitioning: false, timer: null};
  mixRegenerations.set(card, state);
  button.dataset.regenerating = '1';
  button.setAttribute('aria-busy', 'true');
  card.classList.add('is-regenerating');
  Array.from(card.querySelectorAll('.gen-focus .gen-row')).forEach((row, index) => {
    state.animations.push(row.animate([
      { opacity: 1, filter: 'saturate(1)', transform: 'translateX(0)' },
      { opacity: .58, filter: 'saturate(.35)', transform: 'translateX(.22rem)' },
      { opacity: 1, filter: 'saturate(1)', transform: 'translateX(0)' }
    ], { duration: 1250, delay: index * 55, iterations: Infinity, easing: 'ease-in-out' }));
  });
  try { htmx.trigger(button, 'mix-regenerate'); }
  catch (_) { finishMixRegeneration(card, true); }
}

document.addEventListener('htmx:afterRequest', event => {
  const card = event.detail.elt?.closest('.gen-card');
  const state = card && mixRegenerations.get(card);
  if (!state || state.button !== event.detail.elt) return;
  // A successful response finishes after the custom transition. Every other completion,
  // including a network error or a response that never reached beforeSwap, releases the guard.
  if (!event.detail.successful || !state.transitioning) finishMixRegeneration(card, true);
});

document.addEventListener('htmx:beforeSwap', (event) => {
  const target = event.detail.target;
  if (!target || !target.matches('.gen-card-body')) return;
  const card = target.closest('.gen-card.is-regenerating');
  const state = card && mixRegenerations.get(card);
  if (!state) return;
  event.detail.shouldSwap = false;
  const response = new DOMParser().parseFromString(event.detail.serverResponse, 'text/html');
  const replacement = response.getElementById(target.id);
  if (event.detail.xhr.status < 200 || event.detail.xhr.status >= 300
      || !replacement?.matches('.gen-card-body') || !replacement.querySelector('.gen-head')) {
    finishMixRegeneration(card, true);
    return;
  }

  state.transitioning = true;
  state.animations.forEach(animation => animation.cancel());
  state.animations = [];
  const rows = Array.from(target.querySelectorAll('.gen-focus .gen-row'));
  const step = 38;
  const duration = 280;
  rows.forEach((row, index) => {
    state.animations.push(row.animate([
      { opacity: 1, transform: 'translateX(0) scale(1)', filter: 'blur(0)' },
      { opacity: 0, transform: 'translateX(1.25rem) scale(.97)', filter: 'blur(5px)' }
    ], { duration, delay: index * step, easing: 'cubic-bezier(.55,0,.8,.35)', fill: 'both' }));
  });

  const sequenceLength = duration + Math.max(0, rows.length - 1) * step;
  state.timer = window.setTimeout(() => {
    const incoming = Array.from(replacement.querySelectorAll('.gen-focus .gen-row'));
    incoming.forEach((row) => { row.style.opacity = '0'; });
    target.replaceChildren(...replacement.childNodes);
    target.dataset.name = replacement.dataset.name || '';
    target.dataset.genres = replacement.dataset.genres || '[]';
    if (replacement.dataset.recipe) target.dataset.recipe = replacement.dataset.recipe;
    else delete target.dataset.recipe;
    htmx.process(target);
    const enterStep = 44;
    incoming.forEach((row, index) => {
      const animation = row.animate([
        { opacity: 0, transform: 'translateX(-1rem) scale(.98)', filter: 'blur(5px)' },
        { opacity: 1, transform: 'translateX(0) scale(1)', filter: 'blur(0)' }
      ], { duration: 400, delay: index * enterStep,
           easing: 'cubic-bezier(.18,.78,.2,1)', fill: 'both' });
      animation.finished.then(() => row.style.removeProperty('opacity')).catch(() => {});
    });
    finishMixRegeneration(card);
    // This animated replacement bypasses HTMX's swap/settle events.
    syncHomeThemes();
  }, sequenceLength + 20);
});

function homeStatus() {
  // Home consumes the document's shared playback status. Library syncing is automatic in the
  // background, so the only action here is an
  // unobtrusive "Refresh library" link that kicks a manual POST /sync for power users.
  return {
    connected: false,
    nowPlaying: null,
    radioActive: false,
    radioReady: false,
    get radioPlaybackState() {
      const song = this.nowPlaying;
      if (!this.connected || !this.radioActive || !this.radioReady || !song?.video_id
          || !(song.title || song.artist)
          || song.source?.kind !== 'radio' || typeof song.paused !== 'boolean') return '';
      return song.paused ? 'paused' : 'playing';
    },
    sensorHealth: null,
    refreshing: false,
    running: false,        // a library sync is streaming into the console
    failed: false,
    lines: [],
    _job: null,            // job id the console is attached to (guards against double-attach)
    _scan: null, _stream: null, _reload: null, _disposed: false, _initialized: false,
    destroy() {
      this._disposed = true;
      clearInterval(this._scan); clearTimeout(this._reload);
      this._stream?.close();
    },
    applyBridgeStatus(data) {
      if (this._disposed) return;
      this.connected = !!data.connected;
      this.nowPlaying = data.now_playing ? {...data.now_playing} : null;
      this.radioActive = !!data.radio;
      this.radioReady = !!data.radio_ready;
      this.sensorHealth = data.sensor_health || null;
    },
    init() {
      if (this._initialized) return;
      this._initialized = true;
      window.playbackStatus.start();
      if (window.playbackStatus.snapshot) this.applyBridgeStatus(window.playbackStatus.snapshot);
      // Attach the live console to whatever library sync is running — the automatic background sync
      // included — so the initial sync's results stream in with no manual action.
      const scan = () => { if (this._disposed || this.running) return; fetch('/sync/active').then(r => r.json())
        .then(d => { if (d && d.job_id) this.attach(d.job_id); }).catch(() => {}); };
      scan();
      this._scan = setInterval(scan, 3000);
    },
    push(ev) {
      const bad = ev.type === 'err' || ev.type === 'auth_expired';
      const pip = bad ? '✗' : (ev.type === 'done' || ev.type === 'end') ? '✓' : '•';
      const cls = bad ? 'err' : (ev.type === 'done' || ev.type === 'end') ? 'done' : '';
      this.lines.push({ text: ev.text || '', pip, cls });
      this.$nextTick(() => { const l = this.$refs.log; if (l) l.scrollTop = l.scrollHeight; });
    },
    attach(jobId) {
      if (this._disposed || this.running || this._job === jobId) return;
      this._job = jobId; this.running = true; this.failed = false; this.lines = [];
      this._stream?.close();
      const es = this._stream = new EventSource(`/sync/events/${jobId}`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === 'err' || ev.type === 'auth_expired') this.failed = true;
        if (ev.type === 'end') {
          es.close(); this.running = false;
          if (ev.outcome === 'complete' && !ev.error && !this.failed) {
            this.push({ type: 'done', text: 'library updated — refreshing…' });
            this._reload = setTimeout(() => location.reload(), 900);
          } else {
            this.failed = true;
            const message = ev.outcome === 'partial'
              ? 'Library partially updated. Some resources need another sync.'
              : ev.outcome === 'skipped'
                ? 'Sync skipped. Reconnect YouTube Music and retry.'
                : 'Sync failed. Reconnect or re-authenticate, then retry.';
            this.push({ type: 'err', text: message });
          }
          return;
        }
        this.push(ev);
      };
      es.onerror = () => { es.close(); this.running = false; this.failed = true;
        this.push({ type: 'err', text: 'stream interrupted' }); };
    },
    async refresh() {
      if (this.running || this.refreshing) return;
      this.refreshing = true;
      // Kicking off a sync retires the setup flash (same event the old sync bar dispatched).
      window.dispatchEvent(new CustomEvent('sync-started'));
      if (location.search.includes('flash=')) history.replaceState({}, '', location.pathname);
      try {
        const r = await fetch('/sync', { method: 'POST' });
        const job = (await r.json()).job_id;
        if (job) this.attach(job);          // stream this one into the console
      } catch (e) {}
      this.refreshing = false;
    },
    rate(action) {
      if (!this.nowPlaying) return;
      const want = action === 'like' ? 'LIKE' : 'DISLIKE';
      const current = this.nowPlaying;
      const previous = current.likeStatus;
      // The next poll reconciles with the backend's confirmed rating.
      this.nowPlaying.likeStatus = this.nowPlaying.likeStatus === want ? 'INDIFFERENT' : want;
      fetch('/now-playing/rate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, videoId: this.nowPlaying.video_id,
                               likeStatus: this.nowPlaying.likeStatus }),
      }).then(r => r.json()).then(d => {
        if (!d.ok && this.nowPlaying === current) current.likeStatus = previous;
      }).catch(() => { if (this.nowPlaying === current) current.likeStatus = previous; });
    },
    toggle() {
      if (!this.nowPlaying) return;
      // Optimistic flip; the next poll reconciles with the real state read back from the player.
      this.nowPlaying.paused = !this.nowPlaying.paused;
      fetch('/now-playing/toggle', { method: 'POST' }).catch(() => {});
    },
  };
}
// A genre row's subgenre drill-down toggle. The open/closed state is kept per-family on `window`
// (not localStorage: it's session-scoped, not worth persisting across targeted bar-list swaps).
// Family name comes from
// data-fam (safe for any name; no string interpolation into the expression).
function fpGenre() {
  return {
    open: false,
    fam: '',
    // Capture the family name at init, where $el is the component root (in a method fired from the
    // button's @click, $el would be the button instead, which has no data-fam).
    init() {
      this.fam = this.$el.dataset.fam || '';
      this.open = !!(window.__fpOpen && window.__fpOpen[this.fam]);
    },
    toggle() {
      this.open = !this.open;
      (window.__fpOpen = window.__fpOpen || {})[this.fam] = this.open;
    },
  };
}

// Live center-anchored fill for the Home nudge sliders (#2): update --p (thumb position as a %) on
// drag so the track fills from neutral out to the thumb in real time. Delegated on document so it
// covers sliders re-rendered by htmx swaps; the initial value is set inline per-render server-side.
document.addEventListener('input', function (e) {
  var s = e.target;
  if (s && s.classList && (s.classList.contains('fp-slider') || s.classList.contains('fp-breadth-slider'))) {
    var pct = (s.value - s.min) / (s.max - s.min) * 100;
    s.style.setProperty('--p', pct + '%');
  }
});

// Read actual displayed themes, including a single-card refresh, instead of inferring them from
// the preference sliders or a different browser tab's last request.
function homePoolExclusions() {
  return JSON.stringify(Array.from(
    document.querySelectorAll('#home-mode-cards .gen-row[data-key]'), row => row.dataset.key));
}

function homeOfferedGenres() {
  const genres = new Set();
  // HTMX temporarily preserves a skeleton's CSS classes when it becomes a ready card.
  // Its genre metadata is current immediately, including before the swap settles.
  document.querySelectorAll('#home-mode-cards [data-genres]').forEach(card => {
    try {
      JSON.parse(card.dataset.genres).forEach(g => { if (typeof g === 'string' && g) genres.add(g); });
    } catch (_) { /* A card without metadata cannot establish its genre. */ }
  });
  return [...genres].sort();
}

let mixTuningDeadline = 0;
let mixTuningTimer;
let mixTuningPanel;
let mixTuningRefreshing = false;

function syncMixTuningIndicator(active = null) {
  const panel = document.getElementById('fingerprint');
  if (!panel) return;
  if (active !== null) panel.dataset.tuningActive = active;
  const set = panel.dataset.tuningActive === 'true' ||
    !!panel.querySelector('#home-genre-options input:checked');
  panel.querySelector('.fp-tuning-dot').hidden = !set;
  panel.querySelector('.fp-collapse').title = 'Tune these mixes' + (set ? ' (adjustments active)' : '');
  panel.querySelector('#fp-tuning-status').textContent = set ? 'Mix adjustments are active.' : '';
}

function scheduleMixTuningExpiry(seconds) {
  clearTimeout(mixTuningTimer);
  mixTuningDeadline = seconds > 0 ? Date.now() + seconds * 1000 : 0;
  if (mixTuningDeadline) mixTuningTimer = setTimeout(refreshExpiredMixTuning, seconds * 1000);
}

function refreshExpiredMixTuning() {
  if (mixTuningRefreshing || !document.getElementById('fingerprint')) return;
  if (!mixTuningDeadline || Date.now() < mixTuningDeadline) return;
  mixTuningRefreshing = true;
  // The server checks the deadline again, including changes made in another tab.
  htmx.ajax('GET', '/home/fingerprint', {target: '#fingerprint', swap: 'outerHTML'})
    .catch(() => {})
    .finally(() => {
      mixTuningRefreshing = false;
      if (mixTuningDeadline && Date.now() >= mixTuningDeadline) {
        mixTuningTimer = setTimeout(refreshExpiredMixTuning, 30000);
      }
    });
}

function syncMixTuningExpiry() {
  const panel = document.getElementById('fingerprint');
  if (panel === mixTuningPanel) return;
  mixTuningPanel = panel;
  scheduleMixTuningExpiry(Number(panel?.dataset.tuningRemaining || 0));
}

document.addEventListener('htmx:beforeCleanupElement', event => {
  const element = event.detail.elt;
  if (element === discoveryExposure?.home) {
    discoveryExposure.observer.disconnect();
    discoveryExposure.timers.forEach(clearTimeout);
    discoveryExposure = null;
  }
  if (element === mixTuningPanel) {
    clearTimeout(mixTuningTimer);
    mixTuningPanel = null;
    mixTuningDeadline = 0;
  }
  if (element.matches?.('.gen-card')) finishMixRegeneration(element);
});

document.addEventListener('DOMContentLoaded', syncMixTuningExpiry);
document.addEventListener('htmx:afterSettle', syncMixTuningExpiry);
document.addEventListener('htmx:afterRequest', event => {
  syncMixTuningIndicator(event.detail.xhr?.getResponseHeader('X-Mix-Tuning-Active') ?? null);
  const raw = event.detail.xhr?.getResponseHeader('X-Mix-Tuning-Remaining');
  if (raw === null || raw === undefined) return;
  const remaining = Number(raw);
  if (remaining === 0 && mixTuningDeadline && event.detail.target?.id !== 'fingerprint') {
    // A fragment can notice expiration before the timer; refresh its controls too.
    mixTuningDeadline = Date.now();
    refreshExpiredMixTuning();
  } else {
    scheduleMixTuningExpiry(remaining);
  }
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshExpiredMixTuning();
});
window.addEventListener('focus', refreshExpiredMixTuning);

function syncHomeThemes() {
  syncHomeVibe();
  const summary = document.getElementById('home-theme-summary');
  const options = document.getElementById('home-genre-options');
  if (!summary || !options) return;
  const row = document.getElementById('home-mode-cards');
  // A new menu restores its applied choices. Unrelated swaps preserve drafts and keyboard focus.
  const signature = JSON.stringify([row?.dataset.epoch, row?.dataset.genreOptions, row?.dataset.requestedGenres]);
  if (options.dataset.signature !== signature) {
    options.dataset.signature = signature;
    const available = JSON.parse(row?.dataset.genreOptions || '[]');
    const selected = JSON.parse(row?.dataset.requestedGenres || '[]');
    options.replaceChildren();
    available.forEach(genre => {
      const label = document.createElement('label');
      label.className = 'fp-genre-choice';
      label.style.setProperty('--genre-tint', genre.tint || 'var(--genre-default)');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'genres';
      input.value = genre.family;
      input.setAttribute('aria-label', genre.label);
      input.checked = selected.includes(genre.family);
      const name = document.createElement('span');
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 16 16');
      icon.setAttribute('aria-hidden', 'true');
      icon.setAttribute('focusable', 'false');
      for (const [kind, d] of [['add', 'M8 3.5v9M3.5 8h9'], ['check', 'm3.5 8 3 3 6-6']]) {
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('class', 'fp-genre-' + kind);
        path.setAttribute('d', d);
        icon.append(path);
      }
      name.append(icon, document.createTextNode(genre.label));
      label.append(input, name);
      options.append(label);
    });
    delete summary.dataset.failed;
    filterHomeGenres();
  }
  if (document.getElementById('fp-mixes-panel')?.getAttribute('aria-busy') === 'true') return;
  if (!summary.dataset.failed) {
    summary.textContent = row?.dataset.genreWarning || '';
  }
  updateHomeGenreSelection();
}

function filterHomeGenres() {
  const query = document.getElementById('home-genre-search')?.value.trim().toLowerCase() || '';
  const choices = [...document.querySelectorAll('#home-genre-options .fp-genre-choice')];
  const tokens = query.split(/[\s-]+/).filter(Boolean);
  choices.forEach((choice, index) => {
    choice.hidden = query ? !tokens.every(token => choice.textContent.toLowerCase().includes(token))
      : index >= 12 && !choice.querySelector('input').checked;
  });
  const heading = document.getElementById('home-genre-heading');
  if (heading) heading.textContent = query ? 'Matching genres' : 'Your top genres';
  const empty = document.getElementById('home-genre-empty');
  if (empty) {
    empty.hidden = choices.some(choice => !choice.hidden);
    empty.textContent = choices.length ? 'No matching genres.' : 'Genres will appear when your mixes are ready.';
  }
}

function updateHomeGenreSelection() {
  const form = document.getElementById('home-mix-form');
  if (!form) return;
  const count = form.querySelectorAll('input[name="genres"]:checked').length;
  const busy = document.getElementById('fp-mixes-panel')?.getAttribute('aria-busy') === 'true';
  document.getElementById('home-make-mixes').disabled = busy || (!count && !homeOfferedGenres().length);
  form.querySelector('.fp-submit-label').textContent = count ? 'Make mixes' : 'Shuffle genres';
  const note = document.getElementById('home-genre-count');
  if (note) note.textContent = count ? count + (count === 1 ? ' genre in the mix.' : ' genres in the mix.')
    : 'Room for a happy accident.';
  syncMixTuningIndicator();
}
document.addEventListener('DOMContentLoaded', syncHomeThemes);
document.addEventListener('htmx:afterSettle', syncHomeThemes);
document.addEventListener('change', event => {
  if (event.target.matches('#home-genre-options input')) {
    updateHomeGenreSelection();
    filterHomeGenres();
  }
});
document.addEventListener('htmx:configRequest', event => {
  if (event.detail.elt?.id !== 'home-mix-form') return;
  if (!event.detail.elt.querySelector('input[name="genres"]:checked')) {
    event.detail.path = '/home/other-genres';
    event.detail.parameters.genres = JSON.stringify(homeOfferedGenres());
  }
});
document.addEventListener('htmx:beforeRequest', event => {
  if (event.detail.elt?.id !== 'home-mix-form') return;
  document.getElementById('fp-mixes-panel')?.setAttribute('aria-busy', 'true');
  delete document.getElementById('home-theme-summary').dataset.failed;
  updateHomeGenreSelection();
  document.getElementById('home-theme-summary').textContent = '';
});
document.addEventListener('htmx:afterRequest', event => {
  if (event.detail.elt?.id !== 'home-mix-form') return;
  document.getElementById('fp-mixes-panel')?.removeAttribute('aria-busy');
  if (!event.detail.successful) {
    const summary = document.getElementById('home-theme-summary');
    summary.dataset.failed = 'true';
    summary.textContent = 'Couldn’t make your mixes. Try again.';
  }
  syncHomeThemes();
  if (event.detail.successful && !document.getElementById('home-mode-cards')?.dataset.genreWarning
      && document.querySelector('#home-mode-cards [data-genres]')) {
    window.dispatchEvent(new CustomEvent('home-mixes-applied'));
  }
});

function syncHomeVibe() {
  const panel = document.getElementById('fingerprint');
  if (!panel) return;
  const row = document.getElementById('home-mode-cards');
  const vibe = row?.dataset.vibe ?? panel.dataset.vibe ?? '';
  panel.querySelectorAll('#home-vibe-form input').forEach(input => { input.checked = input.value === vibe; });
  window.dispatchEvent(new CustomEvent('home-vibe-sync', {detail: vibe}));
  const status = document.getElementById('home-vibe-status');
  if (status && !status.dataset.failed && !panel.querySelector('#home-vibe-form.htmx-request')) {
    const label = panel.querySelector('#home-vibe-form input:checked')?.closest('label').querySelector('strong')?.textContent;
    status.textContent = label ? 'In the mix: ' + label + '.' : 'Pick a vibe. Your mixes update right away.';
  }
}

document.addEventListener('htmx:beforeRequest', event => {
  if (event.detail.elt?.id !== 'home-vibe-form') return;
  const status = document.getElementById('home-vibe-status');
  delete status.dataset.failed;
  status.textContent = 'Finding your kind of ' + (event.detail.elt.querySelector('input:checked')?.value ? 'vibe…' : 'music…');
});
document.addEventListener('htmx:afterRequest', event => {
  if (event.detail.elt?.id === 'home-vibe-form') {
    const status = document.getElementById('home-vibe-status');
    if (!event.detail.successful) {
      status.dataset.failed = 'true';
      status.textContent = 'Couldn’t change the vibe. Try again.';
    }
    syncHomeVibe();
    if (event.detail.successful) window.dispatchEvent(new CustomEvent('mixes-reranked'));
  }
  if (event.detail.elt?.matches('.fp-slider, .fp-breadth-slider')) {
    const status = document.getElementById('home-preference-status');
    if (status) status.textContent = event.detail.successful ? 'Freshly adjusted. Find your next mix.'
      : 'Couldn’t update your mixes. Try that adjustment again.';
  }
});

// Remember the taste panel's open/closed state across page loads and fragment swaps.
function fpPanel() {
  return {
    // Power-user control: compact on first encounter; an explicit expansion remains remembered.
    collapsed: localStorage.getItem('fp_collapsed') !== '0',
    tab: ['vibe', 'mixes', 'preferences'].includes(sessionStorage.getItem('fp_tuning_tab')) ? sessionStorage.getItem('fp_tuning_tab') : 'vibe',
    vibe: '',
    init() {
      this.vibe = this.$el.dataset.vibe || '';
      if (!this.collapsed) this.$nextTick(() => this.show());
    },
    selectTab(name, focus = false) {
      this.tab = name;
      sessionStorage.setItem('fp_tuning_tab', name);
      if (focus) this.$nextTick(() => this.$refs[name + 'Tab'].focus());
    },
    moveTab(direction) {
      const tabs = ['vibe', 'mixes', 'preferences'];
      this.selectTab(tabs[(tabs.indexOf(this.tab) + direction + tabs.length) % tabs.length], true);
    },
    show() {
      this.collapsed = false;
      localStorage.setItem('fp_collapsed', '0');
      if (!this.$refs.dialog.open) this.$refs.dialog.showModal();
      this.$refs[this.tab + 'Tab'].focus();
    },
    close() {
      const wasOpen = this.$refs.dialog.open;
      this.$refs.dialog.close();
      this.collapsed = true;
      localStorage.setItem('fp_collapsed', '1');
      if (wasOpen) this.$refs.launcher.focus({preventScroll: true});
    },
    dismissBackdrop(event) {
      if (event.target !== this.$refs.dialog) return;
      const rect = this.$refs.dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right ||
          event.clientY < rect.top || event.clientY > rect.bottom) this.close();
    },
    trapFocus(event) {
      const controls = [...this.$refs.dialog.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
        .filter(el => el.tabIndex >= 0 && !el.matches(':disabled') && el.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if ((event.shiftKey && document.activeElement === first) ||
          (!event.shiftKey && document.activeElement === last)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
      }
    },
  };
}

// Home taste-bar genre picker: an autosuggest combo (same widget family as the Clusters genre
// filter) for pinning a steerable genre bar. The full taxonomy is fetched once and cached on
// `window`, then filtered CLIENT-SIDE so typing, reset, and close are instant (the old server-htmx
// search left stale "Add" rows behind on clear).
//
// A pick POSTs to /home/fingerprint/add and swaps ONLY #fp-genre-bars with the server's re-rendered
// bars (the new one included), then re-processes that subtree for htmx. Deliberately a plain fetch +
// innerHTML (not htmx.ajax targeting #home-feed): swapping the whole feed would destroy and recreate
// THIS component on every add, and that destroy/re-init churn is what made adds land only sometimes.
// Keeping the picker outside the swapped region makes every add reliable.
function genrePicker() {
  return {
    opts: [], query: '', open: false, sel: -1,
    load() {
      if (window.__genreOpts) { this.opts = window.__genreOpts; return; }
      fetch('/home/genres').then(r => r.json())
        .then(d => { window.__genreOpts = d.options || []; this.opts = window.__genreOpts; })
        .catch(() => {});
    },
    suggest() {
      const q = this.query.trim().toLowerCase();
      if (!q) return this.opts.slice(0, 10);                 // favorite genres before typing
      // Tokenized, order-independent match: every word you type must appear somewhere in the name, so
      // "rock post", "rock-post" and "post rock" all find "post-rock" (a plain substring wouldn't).
      const toks = q.split(/[^a-z0-9]+/).filter(Boolean);
      return this.opts.filter(o => { const n = o.name.toLowerCase(); return toks.every(t => n.includes(t)); })
        .slice(0, 10);
    },
    move(d) { const n = this.suggest().length; if (n) this.sel = (this.sel + d + n) % n; },
    choose() { const o = this.suggest()[this.sel >= 0 ? this.sel : 0]; if (o) this.pick(o.name); },
    pick(name) {
      this.reset();
      fetch('/home/fingerprint/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'axis=genre:' + encodeURIComponent(name),
      }).then(r => {
        syncMixTuningIndicator(r.headers.get('X-Mix-Tuning-Active'));
        const remaining = r.headers.get('X-Mix-Tuning-Remaining');
        if (remaining !== null) scheduleMixTuningExpiry(Number(remaining));
        return r.text();
      }).then(html => {
        const bars = document.getElementById('fp-genre-bars');
        if (!bars) return;
        bars.innerHTML = html;                       // server-rendered bars (dedup + order correct)
        if (window.htmx) htmx.process(bars);         // wire the bars' hx-* sliders / expanders
      }).catch(() => {});
    },
    reset() { this.query = ''; this.open = false; this.sel = -1; },
  };
}

