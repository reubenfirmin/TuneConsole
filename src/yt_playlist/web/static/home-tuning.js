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

document.addEventListener('DOMContentLoaded', syncMixTuningExpiry);
document.addEventListener('htmx:beforeCleanupElement', event => {
  if (event.detail.elt !== mixTuningPanel) return;
  clearTimeout(mixTuningTimer);
  mixTuningPanel = null;
  mixTuningDeadline = 0;
});
document.addEventListener('home:mix-settled', syncHomeThemes);
document.addEventListener('htmx:afterSettle', syncMixTuningExpiry);
document.addEventListener('htmx:afterRequest', event => {
  syncMixTuningIndicator(event.detail.xhr?.getResponseHeader('X-Mix-Tuning-Active') ?? null);
  const raw = event.detail.xhr?.getResponseHeader('X-Mix-Tuning-Remaining');
  if (raw === null || raw === undefined) return;
  const remaining = Number(raw);
  if (remaining === 0 && mixTuningDeadline && event.detail.target?.id !== 'fingerprint'
      && event.detail.elt?.id !== 'home-vibe-form') {
    // A fragment can notice expiration before the timer; refresh its controls too.
    mixTuningDeadline = Date.now();
    refreshExpiredMixTuning();
  } else {
    // Clearing a vibe already updates its controls below. A zero deadline here
    // means there are no remaining adjustments, rather than an unseen expiry.
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

let homeVibeUpdate = null;
let homeVibeFeedbackTimer;

function homeVibeLabel(value) {
  return [...document.querySelectorAll('#home-vibe-form input')]
    .find(input => input.value === value)?.closest('label').querySelector('strong')?.textContent.trim() || 'your vibe';
}

function dismissHomeVibeFeedback() {
  if (homeVibeUpdate) homeVibeUpdate.dismissed = true;
  document.getElementById('home-vibe-feedback')?.setAttribute('hidden', '');
}

function clearHomeVibeFeedback() {
  clearTimeout(homeVibeFeedbackTimer);
  homeVibeUpdate = null;
  document.getElementById('home-vibe-feedback')?.setAttribute('hidden', '');
}

function renderHomeVibeFeedback() {
  const update = homeVibeUpdate;
  const feedback = document.getElementById('home-vibe-feedback');
  if (!feedback || !update) return;
  feedback.hidden = !!update.dismissed;
  feedback.dataset.state = update.phase;
  const readyMixes = !!document.querySelector('#home-mode-cards .gen-card-body');
  let title = update.value ? 'Adjusting your mixes for ' + update.label + '…' : 'Adjusting your mixes…';
  let detail = readyMixes ? '' : 'Your mix tapes will appear here when they’re ready.';
  if (update.phase === 'ready') {
    title = update.value ? 'Your ' + update.label + ' mix tapes are ready.' : 'Your mix tapes are ready.';
    detail = '';
  } else if (update.phase === 'error') {
    title = 'Couldn’t update your mix tapes.';
    detail = 'Choose a vibe to try again.';
  } else if (update.phase === 'notice') {
    title = update.notice;
    detail = 'We’ll keep trying to prepare your mix tapes.';
  }
  // Repeated preparation polls must not repeat an unchanged live announcement.
  const heading = document.getElementById('home-vibe-feedback-title');
  const description = document.getElementById('home-vibe-feedback-detail');
  if (heading.textContent !== title) heading.textContent = title;
  if (description.textContent !== detail) description.textContent = detail;
}

function syncHomeVibeFeedback() {
  const feedback = document.getElementById('home-vibe-feedback');
  const row = document.getElementById('home-mode-cards');
  if (!feedback || !row) return;
  // Navigation replaces the feed; a previous page's request must not own its status.
  if (homeVibeUpdate?.feedback !== feedback) {
    clearTimeout(homeVibeFeedbackTimer);
    homeVibeUpdate = null;
  }
  const value = row.dataset.vibe || '';
  if (homeVibeUpdate?.phase === 'saving') return;
  if (homeVibeUpdate?.phase === 'error') {
    if (value === homeVibeUpdate.previousValue) return;
    clearHomeVibeFeedback();
  }
  if (homeVibeUpdate && homeVibeUpdate.value !== value) {
    clearTimeout(homeVibeFeedbackTimer);
    homeVibeUpdate = null;
    feedback.hidden = true;
  }
  // Restore ongoing preparation after a reload, without announcing old completed changes.
  if (!homeVibeUpdate && value && row.dataset.preparing === 'true') {
    homeVibeUpdate = {feedback, value, label: homeVibeLabel(value), phase: 'waiting'};
  }
  if (!homeVibeUpdate) return;
  const phase = row.dataset.preferenceNotice ? 'notice'
    : row.dataset.preparing === 'true' || !row.querySelector('.gen-card-body') ? 'waiting' : 'ready';
  if (phase !== homeVibeUpdate.phase) {
    clearTimeout(homeVibeFeedbackTimer);
    homeVibeUpdate.phase = phase;
    if (phase === 'ready') {
      const completed = homeVibeUpdate;
      homeVibeFeedbackTimer = setTimeout(() => {
        if (homeVibeUpdate === completed) dismissHomeVibeFeedback();
      }, 8000);
    }
  }
  homeVibeUpdate.notice = row.dataset.preferenceNotice;
  renderHomeVibeFeedback();
}

function homeVibeFeedback() {
  return {
    init() { syncHomeVibeFeedback(); },
    destroy() {
      if (homeVibeUpdate?.feedback === this.$el) clearHomeVibeFeedback();
    },
  };
}

function syncHomeVibe() {
  syncHomeVibeFeedback();
  const panel = document.getElementById('fingerprint');
  if (!panel) return;
  const row = document.getElementById('home-mode-cards');
  const vibe = row?.dataset.vibe ?? panel.dataset.vibe ?? '';
  panel.querySelectorAll('#home-vibe-form input').forEach(input => { input.checked = input.value === vibe; });
  window.dispatchEvent(new CustomEvent('home-vibe-sync', {detail: vibe}));
}

document.addEventListener('htmx:beforeRequest', event => {
  if (event.detail.elt?.matches('[hx-post="/home/fingerprint/reset"]')) clearHomeVibeFeedback();
  if (event.detail.elt?.id !== 'home-vibe-form') return;
  const selected = event.detail.elt.querySelector('input:checked');
  clearTimeout(homeVibeFeedbackTimer);
  homeVibeUpdate = {
    feedback: document.getElementById('home-vibe-feedback'),
    request: event.detail.xhr,
    previousValue: document.getElementById('home-mode-cards')?.dataset.vibe || '',
    value: selected?.value || '', label: homeVibeLabel(selected?.value || ''), phase: 'saving',
  };
  window.dispatchEvent(new CustomEvent('home-vibe-saving'));
  renderHomeVibeFeedback();
});
document.addEventListener('htmx:afterRequest', event => {
  if (event.detail.elt?.id === 'home-vibe-form') {
    if (homeVibeUpdate?.request !== event.detail.xhr) return;
    if (homeVibeUpdate?.request === event.detail.xhr) {
      homeVibeUpdate.phase = event.detail.successful ? 'waiting' : 'error';
      renderHomeVibeFeedback();
    }
    syncHomeVibe();
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

