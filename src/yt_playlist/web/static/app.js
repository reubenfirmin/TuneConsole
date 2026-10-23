// Keep --topbar-h in sync with the real navbar height so sticky table headers pin right below it.
function syncTopbarH() {
  const tb = document.querySelector('.topbar');
  if (tb) {
    const rail = getComputedStyle(tb).position === 'fixed' && tb.offsetHeight >= window.innerHeight;
    document.documentElement.style.setProperty('--topbar-h', (rail ? 0 : tb.offsetHeight) + 'px');
  }
}
window.addEventListener('DOMContentLoaded', syncTopbarH);
window.addEventListener('resize', syncTopbarH);

// Home and Road Trip open the same generation experience before their async playlist request.
function genOpenYT(id) {
  try {
    const thumbs = Array.from(document.getElementById(id).querySelectorAll('.gen-row, .rt-row'))
      .map((row) => row.dataset.thumb).filter(Boolean).slice(0, 16);
    localStorage.setItem('tc_gen_thumbs', JSON.stringify(thumbs));
  } catch (_) {}
  try { window.__ytTab = window.open('/home/generating', '_blank'); } catch (_) {}
}

// htmx: a 422 carries an OOB error toast. Process its body while retaining the failed
// request status so optimistic controls roll back. The server sets `HX-Reswap: none` to keep the primary
// target untouched while the OOB toast still lands in #toasts. Bind on `document`
// (not document.body): app.js loads in <head>, before <body> exists.
document.addEventListener('htmx:beforeSwap', (e) => {
  if (e.detail.xhr.status === 422) e.detail.shouldSwap = true;
});

// A suggestion-add response stages its table row in a <template>. Moving it here avoids the browser's
// special table parser relocating a bare out-of-band <tr>, while keeping the original grid tile as a
// fixed-size vacancy so the remaining suggestions do not reflow.
document.addEventListener('htmx:afterSwap', (e) => {
  const tile = e.detail.target.closest && e.detail.target.closest('.tile-suggest');
  const result = tile && tile.querySelector('.suggestion-add-result');
  if (!result) return;
  const template = result.querySelector('.suggestion-track-row');
  const body = document.getElementById('playlist-track-body');
  if (!template || !body) return;
  const row = template.content.firstElementChild;
  if (!row) return;
  body.appendChild(row);
  htmx.process(row);
  const count = document.getElementById('playlist-track-count');
  if (count) count.textContent = result.dataset.trackCount;
  tile.classList.remove('suggestion-departing');
  tile.classList.add('suggestion-vacancy');
  tile.setAttribute('aria-hidden', 'true');
  document.dispatchEvent(new CustomEvent('playlist-track-added'));
});

function mountNowPlayingDock() {
  const card = document.querySelector('.global-now-playing');
  const slot = document.querySelector('.sidebar-dock-slot');
  const origin = document.querySelector('.global-now-playing-slot');
  if (!card || !slot || !origin) return;
  const target = window.matchMedia('(min-width: 65rem)').matches ? slot : origin;
  if (card.parentElement !== target) target.appendChild(card);
  if (!window.__homeDockBound) {
    window.__homeDockBound = true;
    window.addEventListener('resize', mountNowPlayingDock);
  }
}

// Alpine component factories for the various pages (loaded globally via base.html).
function rowSort(pid) {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  // Also hosts playlist editing dialogs, invoked by the shared song menu.
  return {
    pid: pid, key: '', dir: 1,
    _sortable: null,
    destroy() { this._sortable?.destroy(); },
    // alternate-versions modal
    altOpen: false, altLoading: false, altTitle: '',
    sortBy(k) {
      if (this.key === k) { this.dir = -this.dir; } else { this.key = k; this.dir = 1; }
      const tb = this.$refs.body;
      if (!tb) return;
      Array.from(tb.querySelectorAll('tr.srow'))
        .sort((x, y) => {
          const a = x.dataset[k] || '', b = y.dataset[k] || '';
          const na = parseFloat(a), nb = parseFloat(b);
          const numeric = a !== '' && b !== '' && !isNaN(na) && !isNaN(nb);
          return (numeric ? na - nb : a.localeCompare(b)) * this.dir;
        })
        .forEach(r => tb.appendChild(r));
      // A manual drag-reorder only makes sense in the playlist's TRUE order. Once a column sort is
      // applied, the on-screen order isn't canonical, so persisting a single drag would scramble the
      // real order. Disable dragging until the view is reloaded back to the default order.
      if (this._sortable) this._sortable.option('disabled', this.key !== '');
    },
    ind(k) { return this.key === k ? (this.dir === 1 ? 'is-ascending' : 'is-descending') : ''; },

    // Open the modal and let htmx fetch + render the results (server builds the list; we only own the
    // modal open/close + loading flag). The "Add" button hx-includes the checked results.
    findAlternates(vid, title) {
      this.altOpen = true; this.altLoading = true; this.altTitle = title;
      htmx.ajax('GET', `/playlist/${this.pid}/alternates?video_id=${encodeURIComponent(vid)}`,
        { target: '#alt-results', swap: 'innerHTML' }).finally(() => { this.altLoading = false; });
    },

    // remove-track confirmation modal
    rmOpen: false, rmBusy: false, rmErr: '', rmVid: '', rmTitle: '',
    removeTrack(vid, title) {
      this.rmVid = vid; this.rmTitle = title; this.rmErr = ''; this.rmOpen = true;
    },
    async confirmRemove() {
      this.rmBusy = true;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(this.rmVid)}"]`);
      // htmx owns request + swap: success returns an empty row -> the <tr> is removed; an error
      // returns the OOB toast and leaves the row in place.
      try {
        await htmx.ajax('POST', `/playlist/${this.pid}/remove-track`,
          { values: { video_id: this.rmVid }, target: tr, swap: 'outerHTML' });
      } catch (e) { /* leave the row; a reload would resync */ }
      this.rmBusy = false; this.rmOpen = false;
    },

    // --- drag-and-drop reorder via SortableJS (floating drag clone + ghost placeholder) ---
    initSortable() {
      const tb = this.$refs.body;
      if (!tb || typeof Sortable === 'undefined') return;
      this._sortable?.destroy();
      this._sortable = Sortable.create(tb, {
        handle: '.drag-handle',
        disabled: this.key !== '',           // only draggable in the true (unsorted) order
        draggable: 'tr.srow',
        animation: 160,
        // Native drag image (a faithful, full-width snapshot of the row) floats under the cursor.
        // A fallback clone would detach the <tr> from the table and collapse its columns.
        ghostClass: 'srow-ghost',            // the placeholder shown at the insert point
        chosenClass: 'srow-chosen',
        onEnd: (e) => {
          this.renumber();
          if (e.oldIndex === e.newIndex) return;
          const rows = tb.querySelectorAll('tr.srow');
          const moved = e.item.dataset.vid;
          const next = e.item.nextElementSibling;
          const beforeVid = next && next.classList.contains('srow') ? next.dataset.vid : '';
          // htmx persists the new order (no swap: the DOM is already reordered); on failure the
          // server replies HX-Refresh to reload and resync the true order.
          htmx.ajax('POST', `/playlist/${this.pid}/reorder`,
            { values: { video_id: moved, before_video_id: beforeVid }, swap: 'none' });
        },
      });
    },
    renumber() {
      this.$refs.body.querySelectorAll('tr.srow .rownum').forEach((el, i) => { el.textContent = i + 1; });
    },

  };
}
function overlapSort() {
  // Client-side sort of the overlaps table by reordering the per-row <tbody> nodes
  // (which preserves each row's Alpine state, pie menu, hide animation, etc.).
  return {
    key: 'shared', dir: -1,   // default: most-overlapping first
    tail: 0, tailMax: 0, _below: [], confirmOpen: false,
    start() { this.apply(); },
    sortBy(k) {
      if (this.key === k) { this.dir = -this.dir; }
      else { this.key = k; this.dir = (k === 'shared') ? -1 : 1; }
      this.apply();
    },
    apply() {
      const tbl = this.$refs.tbl;
      if (!tbl) return;
      const rows = Array.from(tbl.querySelectorAll('tbody.ov-row'));
      rows.sort((x, y) => {
        if (this.key === 'shared') return (+x.dataset.shared - +y.dataset.shared) * this.dir;
        return (x.dataset[this.key] || '').localeCompare(y.dataset[this.key] || '') * this.dir;
      });
      rows.forEach(r => tbl.appendChild(r));   // re-append in order; moving keeps node state
    },
    // Anchored to a TABLE ROW (not the scroll position): hide this row + every row after it
    // in the current sort order. The control lives on each row, so the cut point is a row.
    dismissFromRow(rowEl) {
      const tbl = this.$refs.tbl;
      if (!tbl || !rowEl) return;
      const rows = Array.from(tbl.querySelectorAll('tbody.ov-row')).filter(r => r.style.display !== 'none');
      const idx = rows.indexOf(rowEl);
      if (idx < 0) return;
      this._below = rows.slice(idx);
      this.tail = this._below.length;
      this.tailMax = this._below.reduce((m, r) => Math.max(m, +r.dataset.shared), 0);
      this.confirmOpen = true;
    },
    // the "hide this + all below" pairs, for the confirm modal's htmx post (see cleanup.html)
    pairsJson() { return JSON.stringify(this._below.map(r => [r.dataset.ay, r.dataset.by])); },
    ind(k) { return this.key === k ? (this.dir === 1 ? 'is-ascending' : 'is-descending') : ''; },
  };
}
function playlistsTab(rows) {
  // Remember the particular row, since New playlists repeats rows from the library below.
  let lastPickedRow = null;
  return {
    rows, activityScale: 0, sel: {}, sortKey: 'title', sortDir: 1, split: false, busy: false,
    groupModal: false, groupName: '', delModal: false, collapsed: { New: false, Generated: true },
    init() {
      this.refreshActivityScale();
      // Alpine watches nested row changes too, including live listen-count updates.
      this.$watch('rows', () => this.refreshActivityScale());
      // remember view preferences across reloads (the tab reloads after group/delete)
      try {
        this.split = localStorage.getItem('pl.split') === '1';
        this.sortKey = localStorage.getItem('pl.sortKey') || 'title';
        this.sortDir = +localStorage.getItem('pl.sortDir') || 1;
        const savedCollapsed = JSON.parse(localStorage.getItem('pl.collapsed') || '{}');
        this.collapsed = { New: false, Generated: true,
          ...(savedCollapsed && typeof savedCollapsed === 'object' ? savedCollapsed : {}) };
      } catch (e) {}
      // Both sections share the same expanded slot; older preferences may have both open.
      if (!this.collapsed.New && !this.collapsed.Generated && this.newRows().length) {
        this.collapsed.Generated = true;
      }
      this.$watch('split', v => { try { localStorage.setItem('pl.split', v ? '1' : '0'); } catch (e) {} });
      this.$watch('sortKey', v => { try { localStorage.setItem('pl.sortKey', v); } catch (e) {} });
      this.$watch('sortDir', v => { try { localStorage.setItem('pl.sortDir', v); } catch (e) {} });
    },
    selected() { return this.rows.filter(r => this.sel[r.id]); },
    count() { return this.selected().length; },
    toggle(id) { this.sel[id] = !this.sel[id]; },
    // checkbox (or its cell) clicked: shift extends the anchor row's state across the visible range
    rowCheck(r, ev) {
      const row = ev?.currentTarget.closest('tr');
      if (ev?.shiftKey && lastPickedRow && lastPickedRow !== row) {
        const visible = [...this.$root.querySelectorAll('tr[data-playlist-id]')]
          .filter(el => el.getClientRects().length);
        const i = visible.indexOf(lastPickedRow), j = visible.indexOf(row);
        if (i !== -1 && j !== -1) {
          const on = !!this.sel[lastPickedRow.dataset.playlistId];
          visible.slice(Math.min(i, j), Math.max(i, j) + 1)
            .forEach(el => { this.sel[el.dataset.playlistId] = on; });
          lastPickedRow = row;
          return;
        }
      }
      this.toggle(r.id);
      lastPickedRow = row;
    },
    sortBy(key) {
      if (this.sortKey === key) { this.sortDir = -this.sortDir; }
      else { this.sortKey = key; this.sortDir = ['count', 'listens', 'last'].includes(key) ? -1 : 1; }
    },
    ind(key) { return this.sortKey === key ? (this.sortDir === 1 ? 'is-ascending' : 'is-descending') : ''; },
    refreshActivityScale() {
      const max = this.rows.reduce((value, row) => Math.max(value, row.listens || 0), 0);
      this.activityScale = Math.log1p(max);
    },
    activity(r) {
      return r.listens && this.activityScale ? Math.log1p(r.listens) / this.activityScale : 0;
    },
    activityLabel(r) {
      const n = r.listens || 0;
      return n.toLocaleString() + ' track play' + (n === 1 ? '' : 's');
    },
    cmp(a, b) {
      const k = this.sortKey, numeric = (k === 'count' || k === 'listens' || k === 'last');
      const r = numeric ? ((a[k] || 0) - (b[k] || 0))
                        : String(a[k] || '').localeCompare(String(b[k] || ''));
      return (r ? r * this.sortDir : a.title.localeCompare(b.title));   // stable tiebreak by title
    },
    sorted() { return [...this.rows].sort((a, b) => this.cmp(a, b)); },
    toggleSection(name) {
      this.collapsed[name] = !this.collapsed[name];
      if (!this.collapsed[name]) this.collapsed[name === 'New' ? 'Generated' : 'New'] = true;
      try { localStorage.setItem('pl.collapsed', JSON.stringify(this.collapsed)); } catch (e) {}
    },
    newRows() {
      return this.rows.filter(r => r.is_new)
        .sort((a, b) => b.created - a.created || b.id - a.id);
    },
    // "Generated" is pinned into its own card above the table (see template), never in the sections.
    // Always newest-first by creation time (independent of the main table's column sort).
    genRows() {
      return this.rows.filter(r => r.is_generated)
        .sort((a, b) => (b.generated_created ?? b.created ?? 0) - (a.generated_created ?? a.created ?? 0)
          || b.id - a.id);
    },
    promote(r) {
      // graduate a Generated playlist into the library (out of the quarantine group), then reload
      fetch('/playlist/' + r.id + '/promote', { method: 'POST', headers: { 'HX-Request': 'true' } })
        .then(() => location.reload());
    },
    sections() {
      // the main table holds everything EXCEPT Generated; split partitions by group (Ungrouped last)
      const rest = this.sorted().filter(r => !r.is_generated);
      if (!this.split) return [{ name: '', rows: rest }];
      const m = {};
      rest.forEach(r => { const g = r.group || 'Ungrouped'; (m[g] = m[g] || []).push(r); });
      return Object.keys(m)
        .sort((a, b) => a === 'Ungrouped' ? 1 : b === 'Ungrouped' ? -1 : a.localeCompare(b))
        .map(n => ({ name: n, rows: m[n] }));
    },
    selectAll(on) { this.rows.forEach(r => { this.sel[r.id] = on; }); },
    fmtLast(ts) {
      if (!ts) return '-';
      const days = Math.floor((Date.now() / 1000 - ts) / 86400);
      if (days <= 0) return 'today';
      if (days === 1) return 'yesterday';
      if (days < 30) return days + 'd ago';
      if (days < 365) return Math.floor(days / 30) + 'mo ago';
      return Math.floor(days / 365) + 'y ago';
    },
    merge() {
      if (this.count() < 2) return;
      location.href = '/merge?ids=' + this.selected().map(r => r.id).join(',') + '&return=/playlists';
    },
    copyModal: false, copyName: '', copyIds: [],
    openCopy() {
      const sel = this.selected();
      if (!sel.length) return;
      this.copyIds = sel.map(r => r.id);
      // single -> "Title (copy)"; multiple -> combine-to-new, prefilled with the joined names
      this.copyName = sel.length === 1 ? sel[0].title + ' (copy)' : sel.map(r => r.title).join(' + ');
      this.copyModal = true;
    },
    copyIntoModal: false, copyIntoTarget: '',
    openCopyInto() {
      const sel = this.selected();
      if (!sel.length) return;
      this.copyIds = sel.map(r => r.id);
      this.copyIntoTarget = '';
      this.copyIntoModal = true;
    },
    // destinations for "Copy into…": every playlist except the selected sources, by title
    intoTargets() {
      const src = new Set(this.copyIds);
      return [...this.rows].filter(r => !src.has(r.id))
        .sort((a, b) => a.title.localeCompare(b.title));
    },
    openGroup() { if (this.count()) { this.groupName = ''; this.groupModal = true; } },
    // Modal "save" actions go through htmx.ajax so the server's HX-Refresh reload (success) and
    // 422 OOB error toast (copy-into) behave exactly as the inline hx-post did, but the values
    // come straight from this component's state, not a global Alpine.$data() reach-in.
    copy() { htmx.ajax('POST', '/playlists/copy', { values: { ids: this.copyIds.join(','), name: this.copyName } }).catch(() => {}); },
    copyInto() { htmx.ajax('POST', '/playlists/copy-into', { values: { ids: this.copyIds.join(','), target: this.copyIntoTarget } }); },
    group() { htmx.ajax('POST', '/playlists/group', { values: { ids: this.selected().map(r => r.id).join(','), name: this.groupName } }); },
    // Delete keeps the modal up with a spinner: one remote YouTube call per playlist, so a batch
    // can take a while. `busy` blocks a second submit; success ends in a full page reload.
    remove() {
      if (this.busy) return;
      this.busy = true;
      htmx.ajax('POST', '/playlists/delete', { values: { ids: this.selected().map(r => r.id).join(',') } })
        .catch(() => { this.busy = false; });
    },
  };
}
function moveTab(fromId, toId, rows) {
  return {
    from: fromId, to: toId, rows, sel: {}, lastChecked: null,
    busy: false, progress: '', error: '',
    init() {
      this.$watch('from', () => { this.sel = {}; this.lastChecked = null; });
    },
    visibleIds() { return this.rows.filter(r => r.identity_id === this.from).map(r => r.id); },
    selectedIds() { return this.visibleIds().filter(id => this.sel[id]); },
    count() { return this.selectedIds().length; },
    allSelected() { return this.visibleIds().length > 0 && this.count() === this.visibleIds().length; },
    canMove() { return this.from != null && this.to != null && this.from !== this.to; },
    selectAll(on) {
      if (this.busy) return;
      this.sel = on ? Object.fromEntries(this.visibleIds().map(id => [id, true])) : {};
      this.lastChecked = null;
    },
    rowCheck(id, event) {
      if (this.busy) return;
      const on = !this.sel[id], ids = this.visibleIds();
      const start = ids.indexOf(this.lastChecked), end = ids.indexOf(id);
      if (event.shiftKey && start >= 0 && end >= 0) {
        ids.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(pid => { this.sel[pid] = on; });
      } else {
        this.sel[id] = on;
      }
      this.lastChecked = id;
    },
    async transfer(copyOnly) {
      if (this.busy || !this.canMove() || !this.count()) return;
      const ids = this.selectedIds();
      this.busy = true;
      this.error = '';
      this.progress = `${copyOnly ? 'Copying' : 'Moving'} ${ids.length} playlist${ids.length === 1 ? '' : 's'}…`;
      try {
        await htmx.ajax('POST', '/move/bulk', {
          target: '#move-panel', swap: 'outerHTML',
          values: { ids: ids.join(','), source_identity: this.from,
            target_identity: this.to, copy_only: copyOnly ? '1' : '' },
        });
      } catch (_) {
        this.error = 'The transfer was interrupted. Check both identities before retrying; some playlists may have been copied or moved.';
      } finally {
        this.busy = false;
      }
    },
  };
}
function titleEditor(pid) {
  // click the playlist <h1> to rename it (YouTube + store). The title stays server-rendered in the
  // <h1> (Jinja escapes it safely); we read/write its text rather than threading it through x-data.
  return {
    pid: pid, editing: false, draft: '',
    start() {
      this.draft = this.$refs.h1.textContent.trim();
      this.editing = true;
      this.$nextTick(() => { this.$refs.inp.focus(); this.$refs.inp.select(); });
    },
  };
}
function enrichPanel(pid, lastfmConfigured, activeJobId, activeSource, enrichBase, conflictCount) {
  // Enrichment: one background job runs the configured provider waterfall and streams over SSE.
  // Updates the Year/Genre cells live as each track resolves, drives a determinate progress bar, and
  // surfaces a conflict count (provider disagreements) that lights up the header's resolve icon.
  // `enrichBase` defaults to /playlist/<pid>; album pages pass /album/<browse>.
  return {
    pid: pid, enrichBase: enrichBase || ('/playlist/' + pid),
    lastfmConfigured: lastfmConfigured, running: false, finished: false, pct: 0, status: '', source: '',
    conflictCount: conflictCount || 0,
    _stream: null, _finishTimer: null, _disposed: false,
    destroy() {
      this._disposed = true;
      this._stream?.close(); clearTimeout(this._finishTimer);
    },
    openConflicts() {
      htmx.ajax('GET', `${this.enrichBase}/conflicts`, { target: '#conflicts-modal', swap: 'innerHTML' });
    },
    // Last.fm API key modal
    keyModal: false, keyValue: '', keyBusy: false, keyErr: '',
    lastfmClick() {
      if (this.lastfmConfigured) this.start('lastfm');
      else { this.keyErr = ''; this.keyModal = true; }
    },
    async saveKey() {
      const key = (this.keyValue || '').trim();
      if (!key) { this.keyErr = 'Enter a key.'; return; }
      this.keyBusy = true; this.keyErr = '';
      try {
        await htmx.ajax('POST', '/settings/lastfm-key', { values: { key }, swap: 'none' });
        this.keyBusy = false;
        this.lastfmConfigured = true; this.keyModal = false; this.keyValue = '';
        this.start('lastfm');                       // saved, run it now
      } catch (e) { this.keyBusy = false; this.keyErr = 'Could not save that key.'; }
    },
    // if the page was refreshed while a job is running, reattach to it and resume the progress UI
    rejoinIfActive() {
      if (activeJobId) {
        this.status = 'Reconnecting to enrichment in progress…';
        this.listen(activeJobId, activeSource);
      }
    },
    async start() {
      if (this.running) return;
      this.source = 'waterfall';
      this.running = true; this.finished = false; this.pct = 0;
      this.status = 'Starting enrichment…';
      let job;
      try {
        const r = await fetch(`${this.enrichBase}/enrich`, { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.status = 'Could not start.'; this.running = false; return; }
      this.listen(job, 'waterfall');
    },
    listen(job, source) {
      if (this._disposed) return;
      this.source = source; this.running = true; this.finished = false;
      clearTimeout(this._finishTimer);
      this._stream?.close();
      const es = this._stream = new EventSource(`/playlist/enrich/events/${job}`);
      let errs = 0;
      es.onmessage = (m) => {
        errs = 0;                          // a delivered event means we're reconnected: reset backoff
        const ev = JSON.parse(m.data);
        if (ev.type === 'track') {
          this.applyRow(ev);
          this.pct = Math.round((ev.i / ev.n) * 100);
          this.status = ev.text;
        } else if (ev.type === 'info') {
          this.status = ev.text;
        } else if (ev.type === 'done') {
          this.pct = 100; this.status = ev.text;
          if (typeof ev.conflicts === 'number') this.conflictCount = ev.conflicts;
        } else if (ev.type === 'err') {
          this.status = ev.text;
          if (ev.text && ev.text.includes('Last.fm API key')) {   // key missing/invalid: prompt for it
            this.lastfmConfigured = false; this.keyModal = true;
          }
        } else if (ev.type === 'end') {
          es.close(); this.running = false; this.finished = true;
          document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
          if (!ev.error) this._finishTimer = setTimeout(() => { this.finished = false; }, 4000);
        }
      };
      es.onerror = () => {
        // A transient drop (e.g. a proxy idle-timeout): let EventSource auto-reconnect. The server
        // replays events idempotently and sends 'end' once the job finishes, so a successful
        // background job no longer looks failed. Give up only after several consecutive failures.
        this.status = 'Reconnecting…';
        if (es.readyState === EventSource.CLOSED || ++errs >= 5) {
          es.close(); this.running = false;
          this.status = 'Progress is unavailable or expired. Reload to check the result.';
        }
      };
    },
    // HTMX owns fragment cleanup and initialization even when SSE supplies the HTML. Alpine's
    // observer handles the removed/inserted trees, as it does for an ordinary HTMX row swap.
    applyRow(ev) {
      if (!ev.row_html) return;
      const tr = document.querySelector(`tr.srow[data-vid="${CSS.escape(ev.video_id)}"]`);
      if (tr) htmx.swap(tr, ev.row_html, { swapStyle: 'outerHTML', settleDelay: 0 });
      document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
    },
  };
}

function genreDialog(trackId, initialChoice) {
  return {
    trackId, choice: initialChoice || '', overriding: false,
    genreList: [], genreSuggestions: [],
    lookupRunning: false, lookupFinished: false, lookupPct: 0, lookupStatus: '',
    openOverride() {
      this.overriding = true;
      if (!this.genreList.length)
        this.genreList = Array.from(document.querySelectorAll('#genrelist option')).map(o => o.value);
      this.genreSuggestions = this.genreList.slice(0, 12);
      this.$nextTick(() => { this.$refs.override.focus(); this.$refs.override.select(); });
    },
    filterGenres() {
      const q = (this.choice || '').trim().toLowerCase();
      this.genreSuggestions = (q ? this.genreList.filter(g => g.toLowerCase().includes(q))
                                 : this.genreList).slice(0, 12);
    },
    async startLookup() {
      if (this.lookupRunning) return;
      this.lookupRunning = true; this.lookupFinished = false; this.lookupPct = 0;
      this.lookupStatus = 'Starting metadata lookup…';
      let job;
      try {
        const response = await fetch(`/track/${this.trackId}/genre-lookup`, { method: 'POST' });
        if (!response.ok) throw new Error('start failed');
        job = (await response.json()).job_id;
      } catch (e) {
        this.lookupRunning = false; this.lookupStatus = 'Could not start lookup.'; return;
      }
      const stream = new EventSource(`/track/genre-lookup/events/${job}`);
      stream.onmessage = (message) => {
        const event = JSON.parse(message.data);
        if (event.type === 'track') {
          this.lookupPct = Math.round((event.i / event.n) * 100); this.lookupStatus = event.text;
        } else if (event.type === 'info' || event.type === 'done' || event.type === 'err') {
          this.lookupStatus = event.text;
          if (event.type === 'done') this.lookupPct = 100;
        } else if (event.type === 'end') {
          stream.close(); this.lookupRunning = false; this.lookupFinished = true;
          if (!event.error) {
            setTimeout(() => htmx.ajax('GET', `/track/${this.trackId}/genre-candidates`,
              { target: '#genre-candidates-modal', swap: 'innerHTML' }), 650);
          }
        }
      };
      stream.onerror = () => {
        stream.close(); this.lookupRunning = false; this.lookupFinished = true;
        this.lookupStatus = 'Lookup stream interrupted.';
      };
    },
  };
}
function authBanner(initial) {
  // The "session expired" bar, seeded from the server, and updated live by the sync panel so it
  // pops up during an AJAX sync (no page reload needed).
  return {
    labels: initial || [],
    dismissed: false,
    add(label) { if (label && !this.labels.includes(label)) this.labels.push(label); },
  };
}
function playerStatus() {
  return {
    connected: false,
    nowPlaying: null,
    metadataOverrides: {},
    pendingRating: null,
    ratingSequence: 0,
    skipping: false,
    artworkFailed: '',
    currentLocation: location.href,
    sourceLinkVisible() {
      const source = this.nowPlaying && this.nowPlaying.source;
      if (!source || source.external || !source.url) return false;
      const target = new URL(source.url, location.href);
      const current = new URL(this.currentLocation);
      if (target.origin !== current.origin) return false;
      if (target.pathname.replace(/\/+$/, '') !== current.pathname.replace(/\/+$/, '')) return true;
      if (source.kind === 'radio') return current.hash !== '#radio';
      return source.kind === 'album' &&
        target.searchParams.get('browse') !== current.searchParams.get('browse');
    },
    sourceName() {
      const kind = this.nowPlaying?.source?.kind || '';
      return kind === 'radio' ? 'Radio' : kind;
    },
    init() {
      window.playbackStatus.start();
      if (window.playbackStatus.snapshot) this.apply(window.playbackStatus.snapshot);
    },
    publishLike() {
      if (!this.nowPlaying || !this.nowPlaying.video_id) return;
      window.dispatchEvent(new CustomEvent('track-like-changed', { detail: {
        videoId: this.nowPlaying.video_id,
        liked: this.nowPlaying.likeStatus === 'LIKE',
      }}));
    },
    updateMetadata(detail) {
      this.metadataOverrides[detail.videoId] = { title: detail.title, artist: detail.artist };
      if (this.nowPlaying?.video_id === detail.videoId) Object.assign(this.nowPlaying, this.metadataOverrides[detail.videoId]);
    },
    apply(d) {
      this.connected = !!d.connected;
      const next = d.now_playing ? { ...d.now_playing } : null;
      if (next) Object.assign(next, this.metadataOverrides[next.video_id]);
      const pending = this.pendingRating;
      if (pending) {
        if (!this.connected || !next || next.video_id !== pending.videoId ||
            next.likeStatus === pending.likeStatus || Date.now() >= pending.expires) {
          this.pendingRating = null;
        } else {
          // A status request can finish before the rating API write. Keep both hearts on the
          // requested state until the backend confirms it, with a bounded wait.
          next.likeStatus = pending.likeStatus;
        }
      }
      this.nowPlaying = next;
      const icon = document.querySelector('link[rel="icon"][data-playing-href]');
      if (icon) {
        icon.dataset.idleHref ||= icon.getAttribute('href');
        const playing = this.connected && next?.paused === false && !!next.video_id;
        const href = playing ? icon.dataset.playingHref : icon.dataset.idleHref;
        if (icon.getAttribute('href') !== href) icon.setAttribute('href', href);
      }
      this.publishLike();
    },
    requestLike(event) {
      const { videoId, liked } = event.detail;
      if (!this.connected || !this.nowPlaying || this.nowPlaying.video_id !== videoId) return;
      // Share the backend rating action with the sidebar so both hearts update together.
      event.preventDefault();
      this.rate('like', liked ? 'LIKE' : 'INDIFFERENT');
    },
    rate(action, desiredStatus = null) {
      if (!this.nowPlaying || this.skipping) return;
      const want = action === 'like' ? 'LIKE' : 'DISLIKE';
      const previous = this.nowPlaying.likeStatus;
      const videoId = this.nowPlaying.video_id;
      const sequence = ++this.ratingSequence;
      this.nowPlaying.likeStatus = desiredStatus || (previous === want ? 'INDIFFERENT' : want);
      const likeStatus = this.nowPlaying.likeStatus;
      const advancing = likeStatus === 'DISLIKE';
      if (advancing) this.skipping = true;
      this.pendingRating = { sequence, videoId, likeStatus: this.nowPlaying.likeStatus,
                             expires: Date.now() + 10000 };
      this.publishLike();
      const revert = () => {
        if (this.pendingRating?.sequence !== sequence) return;
        this.pendingRating = null;
        if (this.nowPlaying?.video_id === videoId) {
          this.nowPlaying.likeStatus = previous;
          this.publishLike();
        }
      };
      fetch('/now-playing/rate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, videoId, likeStatus }) })
        .then(r => r.json()).then(d => {
          if (!d.ok) {
            console.debug('[Rating] update rejected', videoId, d.error);
            revert();
          } else if (d.player_error) {
            console.debug('[Rating] native player update not confirmed', videoId, d.player_error);
          }
        }).catch(error => {
          console.debug('[Rating] request interrupted', videoId, error);
          revert();
        }).finally(() => { if (advancing) this.skipping = false; });
    },
    async next() {
      if (!this.nowPlaying?.video_id || this.skipping) return;
      const videoId = this.nowPlaying.video_id;
      this.skipping = true;
      try {
        const response = await fetch('/now-playing/next', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ videoId }) });
        const result = await response.json();
        if (!result.ok) throw new Error(result.error);
      } catch (error) {
        console.debug('[Playback] skip not yet confirmed', videoId, error);
      } finally {
        this.skipping = false;
      }
    },
    toggle() {
      if (!this.nowPlaying) return;
      this.nowPlaying.paused = !this.nowPlaying.paused;
      fetch('/now-playing/toggle', { method: 'POST' }).catch(() => {});
    },
  };
}

function toggleTrackLike(source, videoId, liked) {
  const request = new CustomEvent('track-like-requested', {
    detail: { videoId, liked }, cancelable: true,
  });
  // The global player handles its current song; every other song uses the library API.
  if (window.dispatchEvent(request)) {
    htmx.ajax('POST', '/track/like', {
      source, values: { video_id: videoId, on: liked ? '1' : '' }, swap: 'none',
    }).catch(() => {}); // the completion event rolls back the heart; the response carries the toast
  }
}
window.mountNowPlayingDock = mountNowPlayingDock;

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

// On a track-list page (playlist, album): highlight the currently-playing row (matched by video id,
// falling back to a normalized title). Metadata enrichment has its own loop, independent of
// playback status, so it continues while the player is disconnected or another list is playing.
(function () {
  if (window.__trackPlaybackBound) return;
  window.__trackPlaybackBound = true;
  function norm(s) {
    return (s || '').toLowerCase().replace(/\(.*?\)|\[.*?\]/g, '').replace(/\bfeat\.?.*$/, '')
      .replace(/[^a-z0-9]+/g, ' ').trim();
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
  function rowByVid(vid) {
    var sel = window.CSS && CSS.escape ? CSS.escape(vid) : vid;
    return document.querySelector('tr[data-vid="' + sel + '"]');
  }
  var metaDone = false, metadataTable = null;
  document.addEventListener('playlist-track-added', function () {
    metaDone = false;
    refreshCells();
    highlightCurrent();
  });
  window.addEventListener('song-metadata-updated', function () {
    document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
  });

  // Returns true if the now-playing track is one of the rows on this page.
  function highlight(np) {
    var rows = document.querySelectorAll('tr[data-vid]');
    rows.forEach(function (r) { r.classList.remove('now-playing'); });
    if (!np || !np.title) return false;
    var match = np.video_id ? rowByVid(np.video_id) : null;
    if (!match) {
      var nt = norm(np.title);
      rows.forEach(function (r) { if (!match && norm(r.getAttribute('data-title')) === nt) match = r; });
    }
    if (match) { match.classList.add('now-playing'); return true; }
    return false;
  }

  function refreshCells() {
    var plId = (location.pathname.match(/^\/playlist\/(\d+)/) || [])[1];
    var table = document.querySelector('tr[data-vid]')?.closest('table');
    if (table !== metadataTable) { metadataTable = table; metaDone = false; }
    if (!plId || !table || metaDone) return;
    fetch('/playlist/' + plId + '/cells').then(function (r) { return r.json(); }).then(function (d) {
      if (!table.isConnected || (location.pathname.match(/^\/playlist\/(\d+)/) || [])[1] !== plId) return;
      (d.cells || []).forEach(function (c) {
        var row = rowByVid(c.video_id); if (!row) return;
        if (c.genre) {
          var g = row.querySelector('.genre-display-wrap');
          if (g && !g.querySelector('.gtag')) {
            var hint = g.querySelector('.ghint');
            if (hint) hint.outerHTML = '<span class="gtag">' + esc(c.genre) + '</span>';
            row.setAttribute('data-genre', String(c.genre).toLowerCase());
          }
        }
        if (c.year) {
          var y = row.querySelector('.ydisplay');
          if (y && y.querySelector('.ghint')) { y.textContent = c.year; row.setAttribute('data-year', String(c.year)); }
        }
      });
      var facets = document.getElementById('fb-facets-' + plId);
      if (facets && d.feedback_version && facets.dataset.metadataVersion !== d.feedback_version) {
        document.body.dispatchEvent(new CustomEvent('playlist-metadata-changed'));
      }
      if (d.pending === 0) metaDone = true;   // everything resolved: stop refreshing cells
    }).catch(function () {});
  }

  function highlightCurrent() {
    var data = window.playbackStatus.snapshot;
    highlight(data?.connected ? data.now_playing : null);
  }
  window.addEventListener('bridge-status', function (event) {
    highlight(event.detail?.connected ? event.detail.now_playing : null);
  });
  document.addEventListener('htmx:afterSwap', highlightCurrent);
  document.addEventListener('DOMContentLoaded', function () { highlightCurrent(); refreshCells(); }, {once: true});
  highlightCurrent();
  refreshCells();
  setInterval(refreshCells, 2500);
})();

// Navbar omnisearch dropdown: open/close + keyboard nav over the HTMX-rendered result rows.
// Visibility is driven by results arriving (htmx:afterSwap, wired in x-init on the form) and by
// focus; we never build markup here. The server owns the dropdown body.
function omniSearch() {
  return {
    open: false,
    active: -1,
    rows() { return Array.from(this.$root.querySelectorAll('.omni-row')); },
    onResults() {
      this.active = -1;
      this.rows().forEach(r => r.classList.remove('active'));
      this.open = this.rows().length > 0;
    },
    close() {
      this.open = false;
      this.active = -1;
      this.rows().forEach(r => r.classList.remove('active'));
    },
    move(dir) {
      const rows = this.rows();
      if (!rows.length) { return; }
      this.open = true;
      this.active = (this.active + dir + rows.length) % rows.length;
      rows.forEach((r, i) => r.classList.toggle('active', i === this.active));
      rows[this.active].scrollIntoView({ block: 'nearest' });
    },
    choose() {
      const rows = this.rows();
      const row = rows[this.active] || rows[0];
      if (row) { row.click(); this.close(); }
    },
  };
}

// ── Taste-model visualization tooltips ──────────────────────────────────────
// A single floating div for plain text or structured data-tip JSON (title + labelled rows).
// Document-level delegation so it also covers the lazily htmx-swapped embedding-engine panel.
function initVizTooltip() {
  let tip = null, current = null, hideTimer = null, plainText = '';
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  // A copy glyph so the user can grab the tooltip's text verbatim (to paste back as an example).
  const COPY_ICON = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<rect x="9" y="9" width="11" height="11" rx="2"/>'
    + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

  function cancelHide() { clearTimeout(hideTimer); }
  function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 240); }
  function hide() { if (tip) tip.classList.remove('on'); current = null; }

  function ensure() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'viz-tip';
      tip.setAttribute('role', 'tooltip');
      // Interactive: the cursor can leave the source and move INTO the tip (to click copy)
      // without it vanishing.
      tip.addEventListener('mouseenter', cancelHide);
      tip.addEventListener('mouseleave', hide);
      tip.addEventListener('click', (e) => {
        const btn = e.target.closest('.vt-copy');
        if (!btn || !navigator.clipboard) return;
        navigator.clipboard.writeText(plainText).then(() => {
          btn.classList.add('done');
          setTimeout(() => btn.classList.remove('done'), 1200);
        }).catch(() => {});
      });
      document.body.appendChild(tip);
    }
    return tip;
  }
  function render(d) {
    if (d.text !== undefined) {
      plainText = String(d.text);
      ensure().textContent = plainText;
      return;
    }
    const dot = /^#[0-9a-fA-F]{3,8}$/.test(d.dot || '') ? d.dot : '';
    const lines = [d.title];
    if (d.desc) lines.push(d.desc);
    let h = '<button class="vt-copy" type="button" title="Copy to clipboard" aria-label="Copy">'
          + COPY_ICON + '</button>';
    h += '<div class="vt-title' + (d.accent ? ' ' + d.accent : '') + '"'
       + (dot ? ' style="--dot:' + dot + '"' : '') + '>' + esc(d.title) + '</div>';
    if (d.desc) h += '<div class="vt-desc">' + esc(d.desc) + '</div>';
    for (const row of (d.rows || [])) {
      const value = row.arrow
        ? String(row.v).split(' to ').map(esc).join(' <span class="vt-change">to</span> ')
        : esc(row.v);
      h += '<div class="vt-row' + (row.div ? ' vt-div' : '') + '">'
         + '<span class="vt-l">' + esc(row.l) + '</span>'
         + '<span class="vt-v' + (row.t ? ' ' + row.t : '') + '">' + value + '</span></div>';
      if (row.n) h += '<div class="vt-n">' + esc(row.n) + '</div>';
      lines.push(row.l + ': ' + row.v + (row.n ? ', ' + row.n : ''));
    }
    plainText = lines.join('\n');
    ensure().innerHTML = h;
  }
  function place(e) {
    if (!tip) return;
    const pad = 16, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = Math.max(8, y) + 'px';
  }

  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) return;
    cancelHide();                       // re-entering a source (or hopping between them) keeps it up
    if (el === current) return;
    let d; try { d = JSON.parse(el.getAttribute('data-tip')); } catch (_) { return; }
    current = el; render(d); ensure().classList.add('on'); place(e);
  });
  // Pinned (no cursor-follow) so it's reachable; a short grace lets the pointer cross into the tip.
  document.addEventListener('mouseout', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (el && el === current && !el.contains(e.relatedTarget)) scheduleHide();
  });
  // Keyboard users get the same structured readout. Place it beside the focused mark rather than
  // requiring pointer coordinates; focusout mirrors the mouseleave grace period.
  document.addEventListener('focusin', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) return;
    cancelHide();
    let d; try { d = JSON.parse(el.getAttribute('data-tip')); } catch (_) { return; }
    current = el; render(d); ensure().classList.add('on');
    const r = el.getBoundingClientRect();
    place({ clientX: r.right, clientY: r.top });
  });
  document.addEventListener('focusout', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (el && el === current && !el.contains(e.relatedTarget)) scheduleHide();
  });
  // A scroll or htmx swap can pull the hovered element out from under the cursor.
  document.addEventListener('scroll', hide, true);
  document.body.addEventListener('htmx:beforeSwap', hide);
}
window.addEventListener('DOMContentLoaded', initVizTooltip);

// hx-confirm -> styled modal (replaces the native window.confirm dialog). htmx fires `htmx:confirm`
// for any element carrying hx-confirm; we intercept it, show our own modal, and only issue the
// request if the user confirms. Reuses the .modal-backdrop/.modal/.modal-actions styles.
document.addEventListener('htmx:confirm', (e) => {
  const question = e.detail.question;
  if (!question) return;                       // no hx-confirm on this element: proceed normally
  e.preventDefault();                          // suppress htmx's native confirm

  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML =
    '<div class="modal" role="dialog" aria-modal="true">' +
    '<h3>Confirm</h3><p></p>' +
    '<div class="modal-actions">' +
    '<button type="button" class="btn-ghost" data-act="cancel">Cancel</button>' +
    '<button type="button" data-act="ok">Confirm</button>' +
    '</div></div>';
  backdrop.querySelector('p').textContent = question;   // textContent: never inject markup

  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (ev) => { if (ev.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); } };
  backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) close(); });
  backdrop.querySelector('[data-act="cancel"]').addEventListener('click', close);
  backdrop.querySelector('[data-act="ok"]').addEventListener('click', () => {
    close();
    document.removeEventListener('keydown', onKey);
    e.detail.issueRequest(true);               // true = skip the confirm check, avoid recursion
  });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(backdrop);
  backdrop.querySelector('[data-act="ok"]').focus();
});
